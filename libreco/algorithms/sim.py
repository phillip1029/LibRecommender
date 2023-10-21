"""Implementation of SIM."""
from ..bases import ModelMeta, TfBase
from ..batch.sequence import get_recent_dual_seqs
from ..layers import dense_nn, embedding_lookup, tf_attention, tf_dense
from ..tfops import dropout_config, reg_config, tf
from ..tfops.features import (
    combine_seq_features,
    compute_dense_feats,
    compute_sparse_feats,
)
from ..torchops import hidden_units_config
from ..utils.misc import count_params
from ..utils.validate import (
    check_dense_values,
    check_multi_sparse,
    check_sparse_indices,
    dense_field_size,
    sparse_feat_size,
    sparse_field_size,
)


class SIM(TfBase, metaclass=ModelMeta):
    """*Search-based Interest Model* algorithm."""

    user_variables = ("embedding/user_embeds_var",)
    item_variables = ("embedding/item_embeds_var",)
    sparse_variables = ("embedding/sparse_embeds_var",)
    dense_variables = ("embedding/dense_embeds_var",)

    def __init__(
        self,
        task,
        data_info=None,
        loss_type="cross_entropy",
        embed_size=16,
        n_epochs=20,
        lr=0.001,
        lr_decay=False,
        epsilon=1e-5,
        reg=None,
        batch_size=256,
        sampler="random",
        num_neg=1,
        use_bn=True,
        dropout_rate=None,
        hidden_units=(200, 80),
        alpha=1.0,
        beta=1.0,
        search_topk=10,
        long_max_len=100,
        short_max_len=10,
        multi_sparse_combiner="sqrtn",
        seed=42,
        lower_upper_bound=None,
        tf_sess_config=None,
    ):
        super().__init__(task, data_info, lower_upper_bound, tf_sess_config)

        self.all_args = locals()
        self.loss_type = loss_type
        self.embed_size = embed_size
        self.n_epochs = n_epochs
        self.lr = lr
        self.lr_decay = lr_decay
        self.epsilon = epsilon
        self.reg = reg_config(reg)
        self.batch_size = batch_size
        self.sampler = sampler
        self.num_neg = num_neg
        self.use_bn = use_bn
        self.dropout_rate = dropout_config(dropout_rate)
        self.hidden_units = hidden_units_config(hidden_units)
        self.alpha = alpha
        self.beta = beta
        self.search_topk = search_topk
        self.long_max_len = long_max_len
        self.short_max_len = short_max_len
        (
            self.cached_long_seqs,
            self.cached_long_lens,
            self.cached_short_seqs,
            self.cached_short_lens,
        ) = get_recent_dual_seqs(
            self.n_users,
            self.user_consumed,
            self.n_items,
            self.long_max_len,
            self.short_max_len,
        )
        self.seed = seed
        self.sparse = check_sparse_indices(data_info)
        self.dense = check_dense_values(data_info)
        if self.sparse:
            self.sparse_feature_size = sparse_feat_size(data_info)
            self.sparse_field_size = sparse_field_size(data_info)
            self.multi_sparse_combiner = check_multi_sparse(
                data_info, multi_sparse_combiner
            )
        if self.dense:
            self.dense_field_size = dense_field_size(data_info)
        self._check_params()

    def _check_params(self):
        assert 0.0 <= self.alpha <= 1.0
        assert 0.0 <= self.beta <= 1.0
        assert self.short_max_len > 0
        assert self.long_max_len >= self.search_topk > 0
        if self.task == "ranking" and self.loss_type not in ("cross_entropy", "focal"):
            raise ValueError(f"unsupported `loss_type`: {self.loss_type}")

    def build_model(self):
        tf.set_random_seed(self.seed)
        self._build_placeholders()
        other_feats = self._build_features()
        # todo: add linear weights to reduce dimension
        self.seq_feats = combine_seq_features(self.data_info, feat_agg_mode="concat")
        # B * K
        self.target_embeds = tf.nn.embedding_lookup(self.seq_feats, self.item_indices)
        # B * seq * K
        self.long_seq_embeds = tf.nn.embedding_lookup(self.seq_feats, self.long_seqs)
        first_stage_out = self._build_first_stage()
        second_stage_out = self._build_second_stage(other_feats)
        self.output = self.alpha * first_stage_out + self.beta * second_stage_out
        self.inference_output = second_stage_out
        self.serving_topk = self.build_topk(second_stage_out)
        count_params()

    def _build_placeholders(self):
        self.user_indices = tf.placeholder(tf.int32, shape=[None])
        self.item_indices = tf.placeholder(tf.int32, shape=[None])
        self.long_seqs = tf.placeholder(tf.int32, shape=[None, self.long_max_len])
        self.long_seq_lens = tf.placeholder(tf.int32, shape=[None])
        self.short_seqs = tf.placeholder(tf.int32, shape=[None, self.short_max_len])
        self.short_seq_lens = tf.placeholder(tf.int32, shape=[None])
        self.labels = tf.placeholder(tf.float32, shape=[None])
        self.is_training = tf.placeholder_with_default(False, shape=[])
        if self.sparse:
            self.sparse_indices = tf.placeholder(
                tf.int32, shape=[None, self.sparse_field_size]
            )
        if self.dense:
            self.dense_values = tf.placeholder(
                tf.float32, shape=[None, self.dense_field_size]
            )

    def _build_first_stage(self):
        seq_mask = tf.sequence_mask(self.long_seq_lens, self.long_max_len)
        seq_mask = tf.tile(
            seq_mask[:, :, tf.newaxis],
            (1, 1, self.long_seq_embeds.shape[-1]),
        )
        paddings = tf.zeros_like(self.long_seq_embeds)
        long_seq_embeds = tf.where(seq_mask, self.long_seq_embeds, paddings)
        pool_seq_embeds = tf.reduce_sum(long_seq_embeds, axis=1, keepdims=False)
        inputs = tf.concat([self.target_embeds, pool_seq_embeds], axis=1)
        mlp_output = dense_nn(
            inputs,
            self.hidden_units,
            use_bn=self.use_bn,
            dropout_rate=self.dropout_rate,
            is_training=self.is_training,
            name="first_stage_mlp",
        )
        return tf.reshape(tf_dense(units=1)(mlp_output), [-1])

    def _build_second_stage(self, other_feats):
        top_k_seq_embeds, top_k_masks = self._gsu_module()
        # todo: multi-head attention
        long_seq_out = self._esu_module(top_k_seq_embeds, top_k_masks)
        short_seq_out = self._din_module()
        inputs = tf.concat([long_seq_out, short_seq_out, other_feats], axis=1)
        mlp_output = dense_nn(
            inputs,
            self.hidden_units,
            use_bn=self.use_bn,
            dropout_rate=self.dropout_rate,
            is_training=self.is_training,
            name="second_stage_mlp",
        )
        return tf.reshape(tf_dense(units=1)(mlp_output), [-1])

    def _gsu_module(self):
        target_embeds = tf.expand_dims(self.target_embeds, axis=1)
        scores = tf.linalg.matmul(target_embeds, self.long_seq_embeds, transpose_b=True)
        scores = tf.squeeze(scores, axis=1)
        seq_mask = tf.sequence_mask(self.long_seq_lens, self.long_max_len)
        paddings = -1e9 * tf.ones_like(scores)
        scores = tf.where(seq_mask, scores, paddings)
        _, indices = tf.math.top_k(scores, self.search_topk, sorted=False)
        # tf.gather vs tf.gather_nd
        # batch_size = tf.shape(target_embeds)[0]
        # nd_indices = tf.stack(
        #    [
        #        tf.repeat(tf.range(batch_size), self.search_topk),
        #        tf.reshape(indices, [-1])
        #    ],
        #    axis=1
        # )
        # nd_indices = tf.reshape(nd_indices, (batch_size, self.search_topk, -1))
        # return tf.gather_nd(self.long_seq_embeds, nd_indices)
        top_k_seq_embeds = tf.gather(self.long_seq_embeds, indices, batch_dims=1)
        top_k_masks = tf.gather(seq_mask, indices, axis=1, batch_dims=1)
        # top_k_masks = tf.gather(seq_mask, indices, batch_dims=-1)
        return top_k_seq_embeds, top_k_masks

    def _esu_module(self, top_k_seq_embeds, top_k_masks):
        return tf_attention(self.target_embeds, top_k_seq_embeds, top_k_masks)

    def _din_module(self):
        short_seq_embeds = tf.nn.embedding_lookup(self.seq_feats, self.short_seqs)
        seq_mask = tf.sequence_mask(self.short_seq_lens, self.short_max_len)
        return tf_attention(self.target_embeds, short_seq_embeds, seq_mask)

    def _build_features(self):
        user_embed = embedding_lookup(
            indices=self.user_indices,
            var_name="user_embeds_var",
            var_shape=(self.n_users + 1, self.embed_size),
            initializer=tf.glorot_uniform_initializer(),
            regularizer=self.reg,
        )
        item_embed = embedding_lookup(
            indices=self.item_indices,
            var_name="item_embeds_var",
            var_shape=(self.n_items + 1, self.embed_size),
            initializer=tf.glorot_uniform_initializer(),
            regularizer=self.reg,
        )
        concat_embeds = [user_embed, item_embed]

        if self.sparse:
            sparse_embeds = compute_sparse_feats(
                self.data_info,
                self.multi_sparse_combiner,
                self.sparse_indices,
                var_name="sparse_embeds_var",
                var_shape=(self.sparse_feature_size, self.embed_size),
                initializer=tf.glorot_uniform_initializer(),
                regularizer=self.reg,
                flatten=True,
            )
            concat_embeds.append(sparse_embeds)
        if self.dense:
            dense_embeds = compute_dense_feats(
                self.dense_values,
                var_name="dense_embeds_var",
                var_shape=(self.dense_field_size, self.embed_size),
                initializer=tf.glorot_uniform_initializer(),
                regularizer=self.reg,
                flatten=True,
            )
            concat_embeds.append(dense_embeds)
        return tf.concat(concat_embeds, axis=1)
