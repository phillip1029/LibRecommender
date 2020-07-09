import random
import time
from operator import itemgetter
from itertools import islice, takewhile
from collections import defaultdict
import numpy as np
from scipy.sparse import issparse
from .base import Base
from ..utils.similarities import cosine_sim, pearson_sim, jaccard_sim
from ..utils.misc import time_block, colorize
from ..evaluate.evaluate import EvalMixin


class UserCF(Base, EvalMixin):
    def __init__(self, task, data_info, sim_type="cosine", k=20,
                 lower_upper_bound=None):

        Base.__init__(self, task, data_info, lower_upper_bound)
        EvalMixin.__init__(self, task)

        self.task = task
        self.k = k
        self.default_prediction = data_info.global_mean if (
                task == "rating") else 0.0
        self.n_users = data_info.n_users
        self.n_items = data_info.n_items
        self.sim_type = sim_type
        self.user_consumed = None
        # sparse matrix, user as row and item as column
        self.user_interaction = None
        # sparse matrix, item as row and user as column
        self.item_interaction = None
        # sparse similarity matrix
        self.sim_matrix = None
        self.print_count = 0
        self._caution_sim_type()

    def fit(self, train_data, block_size=None, num_threads=1, min_common=1,
            mode="invert", verbose=1, eval_data=None, metrics=None):
        self.show_start_time()
        self.user_interaction = train_data.sparse_interaction
        self.item_interaction = self.user_interaction.T.tocsr()
        self.user_consumed = train_data.user_consumed

        with time_block("sim_matrix", verbose=1):
            if self.sim_type == "cosine":
                sim_func = cosine_sim
            elif self.sim_type == "pearson":
                sim_func = pearson_sim
            elif self.sim_type == "jaccard":
                sim_func = jaccard_sim
            else:
                raise ValueError("sim_type must be one of "
                                 "('cosine', 'pearson', 'jaccard')")

            self.sim_matrix = sim_func(
                self.user_interaction, self.item_interaction, self.n_users,
                self.n_items, block_size, num_threads, min_common, mode)

        assert self.sim_matrix.has_sorted_indices
        if issparse(self.sim_matrix):
            n_elements = self.sim_matrix.getnnz()
            sparsity_ratio = 100*n_elements / (self.n_users*self.n_users)
            print(f"sim_matrix, shape: {self.sim_matrix.shape}, "
                  f"num_elements: {n_elements}, "
                  f"sparsity: {sparsity_ratio:5.4f} %")

        if verbose > 1:
            self.print_metrics(eval_data=eval_data, metrics=metrics)
            print("=" * 30)

    def predict(self, user, item):
        user = (np.asarray([user])
                if isinstance(user, int)
                else np.asarray(user))
        item = (np.asarray([item])
                if isinstance(item, int)
                else np.asarray(item))
        unknown_num, unknown_index, user, item = self._check_unknown(
            user, item)

        preds = []
        sim_matrix = self.sim_matrix
        interaction = self.item_interaction
        for u, i in zip(user, item):
            user_slice = slice(sim_matrix.indptr[u], sim_matrix.indptr[u+1])
            sim_users = sim_matrix.indices[user_slice]
            sim_values = sim_matrix.data[user_slice]

            item_slice = slice(interaction.indptr[i], interaction.indptr[i+1])
            item_interacted_u = interaction.indices[item_slice]
            item_interacted_values = interaction.data[item_slice]
            common_users, indices_in_u, indices_in_i = np.intersect1d(
                sim_users, item_interacted_u,
                assume_unique=True, return_indices=True)

            common_sims = sim_values[indices_in_u]
            common_labels = item_interacted_values[indices_in_i]
            if common_users.size == 0 or np.all(common_sims <= 0.0):
                self.print_count += 1
                no_str = (f"No common interaction or similar neighbor "
                          f"for user {u} and item {i}, "
                          f"proceed with default prediction")
                if self.print_count < 13:
                    print(f"{colorize(no_str, 'red')}")
                preds.append(self.default_prediction)
            else:
                k_neighbor_labels, k_neighbor_sims = zip(*islice(
                    takewhile(lambda x: x[1] > 0,
                              sorted(zip(common_labels, common_sims),
                                     key=itemgetter(1),
                                     reverse=True)
                              ),
                    self.k))

                if self.task == "rating":
                    sims_distribution = (
                            k_neighbor_sims / np.sum(k_neighbor_sims)
                    )
                    weighted_pred = np.average(
                        k_neighbor_labels, weights=sims_distribution
                    )
                    preds.append(
                        np.clip(weighted_pred, self.lower_bound,
                                self.upper_bound)
                    )
                elif self.task == "ranking":
                    preds.append(np.mean(k_neighbor_sims))

        if unknown_num > 0:
            preds[unknown_index] = self.default_prediction

        return preds[0] if len(user) == 1 else preds

    def recommend_user(self, user, n_rec, random_rec=False):
        user = self._check_unknown_user(user)
        if not user:
            return   # popular ?

        user_slice = slice(self.sim_matrix.indptr[user],
                           self.sim_matrix.indptr[user+1])
        sim_users = self.sim_matrix.indices[user_slice]
        sim_values = self.sim_matrix.data[user_slice]
        # TODO: return popular items
        if sim_users.size == 0 or np.all(sim_values <= 0):
            self.print_count += 1
            no_str = (f"no similar neighbor for user {user}, "
                      f"return default recommendation")
            if self.print_count < 24:
                print(f"{colorize(no_str, 'red')}")
            return -1

        k_nbs_and_sims = islice(
            sorted(zip(sim_users, sim_values),
                   key=itemgetter(1), reverse=True),
            self.k)
        u_consumed = set(self.user_consumed[user])

        all_item_indices = self.user_interaction.indices
        all_item_indptr = self.user_interaction.indptr
        all_item_values = self.user_interaction.data

        result = defaultdict(lambda: [0.0, 0])  # [sim, count]
        for n, n_sim in k_nbs_and_sims:
            item_slices = slice(all_item_indptr[n], all_item_indptr[n+1])
            n_interacted_items = all_item_indices[item_slices]
            n_interacted_values = all_item_values[item_slices]
            for i, v in zip(n_interacted_items, n_interacted_values):
                if i in u_consumed:
                    continue
                result[i][0] += n_sim * v
                result[i][1] += n_sim

        rank_items = [(k, round(v[0] / v[1], 4)) for k, v in result.items()]
        rank_items.sort(key=lambda x: -x[1])
        if random_rec:
            if len(rank_items) < n_rec:
                item_candidates = rank_items
            else:
                item_candidates = random.sample(rank_items, k=n_rec)
            return item_candidates
        else:
            return rank_items[:n_rec]

    def _caution_sim_type(self):
        caution_str = (f"Warning: {self.sim_type} is not suitable "
                       f"for implicit data")
        if self.task == "ranking" and self.sim_type == "pearson":
            print(f"{colorize(caution_str, 'red')}")
        if self.task == "rating" and self.sim_type == "jaccard":
            print(f"{colorize(caution_str, 'red')}")

