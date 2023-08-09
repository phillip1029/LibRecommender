"""Transformed Dataset."""
from collections import defaultdict
from random import seed as set_random_seed

import numpy as np
from scipy.sparse import csr_matrix

from ..sampling import negatives_from_unconsumed


class TransformedSet:
    """Dataset after transforming.

    Often generated by calling functions in ``DatasetPure`` or ``DatasetFeat``,
    then ``TransformedSet`` will be used in formal training.

    Parameters
    ----------
    user_indices : numpy.ndarray
        All user rows in data, represented in inner id.
    item_indices : numpy.ndarray
        All item rows in data, represented in inner id.
    labels : numpy.ndarray
        All labels in data.
    sparse_indices : numpy.ndarray or None, default: None
        All sparse rows in data, represented in inner id.
    dense_values : numpy.ndarray or None, default: None
        All dense rows in data.

    See Also
    --------
    :class:`~libreco.data.dataset.DatasetPure`
    :class:`~libreco.data.dataset.DatasetFeat`
    """

    def __init__(
        self,
        user_indices=None,
        item_indices=None,
        labels=None,
        sparse_indices=None,
        dense_values=None,
    ):
        self._user_indices = user_indices
        self._item_indices = item_indices
        self._labels = labels
        self._sparse_indices = sparse_indices
        self._dense_values = dense_values
        self._sparse_interaction = csr_matrix(
            (labels, (user_indices, item_indices)), dtype=np.float32
        )

    def __len__(self):
        return len(self.labels)

    def __getitem__(self, index):
        """Get a slice of data."""
        return self.user_indices[index], self.item_indices[index], self.labels[index]

    @property
    def user_indices(self):
        """All user rows in data"""
        return self._user_indices

    @property
    def item_indices(self):
        """All item rows in data"""
        return self._item_indices

    @property
    def sparse_indices(self):
        """All sparse rows in data"""
        return self._sparse_indices

    @property
    def dense_values(self):
        """All dense rows in data"""
        return self._dense_values

    @property
    def labels(self):
        """All labels in data"""
        return self._labels

    @property
    def sparse_interaction(self):
        """User-item interaction data, in :class:`scipy.sparse.csr_matrix` format."""
        return self._sparse_interaction


class TransformedEvalSet:
    """Dataset after transforming.

    Often generated by calling functions in ``DatasetPure`` or ``DatasetFeat``,
    then ``TransformedEvalSet`` will be used in evaluation and testing.

    Parameters
    ----------
    user_indices : numpy.ndarray
        All user rows in data, represented in inner id.
    item_indices : numpy.ndarray
        All item rows in data, represented in inner id.
    labels : numpy.ndarray
        All labels in data.
    """

    def __init__(self, user_indices, item_indices, labels):
        self.user_indices = user_indices
        self.item_indices = item_indices
        self.labels = labels
        self.has_sampled = False
        self.positive_consumed = self._get_positive_consumed()

    def _get_positive_consumed(self):
        # data without label column has dummy labels 0
        label_all_positive = np.all(np.asarray(self.labels) == 0)
        user_consumed = defaultdict(list)
        for u, i, lb in zip(self.user_indices, self.item_indices, self.labels):
            if label_all_positive or lb != 0:
                if isinstance(u, np.integer):
                    u = u.item()
                if isinstance(i, np.integer):
                    i = i.item()
                user_consumed[u].append(i)
        return {u: np.unique(items).tolist() for u, items in user_consumed.items()}

    def build_negatives(self, n_items, num_neg, seed):
        """Perform negative sampling on all the data contained.

        Parameters
        ----------
        n_items : int
            Number of total items.
        num_neg : int
            Number of negative samples for each positive sample.
        seed : int
            Random seed.
        """
        set_random_seed(seed)
        self.has_sampled = True
        # use original users and items to sample
        items_neg = self._sample_neg_items(
            self.user_indices, self.item_indices, n_items, num_neg
        )
        self.user_indices = np.repeat(self.user_indices, num_neg + 1)
        self.item_indices = np.repeat(self.item_indices, num_neg + 1)
        self.labels = np.zeros_like(self.item_indices, dtype=np.float32)
        self.labels[:: (num_neg + 1)] = 1.0

        assert len(self.item_indices) == len(items_neg) * (num_neg + 1) / num_neg
        for i in range(num_neg):
            self.item_indices[(i + 1) :: (num_neg + 1)] = items_neg[i::num_neg]

    def _sample_neg_items(self, users, items, n_items, num_neg):
        user_consumed_set = {u: set(uis) for u, uis in self.positive_consumed.items()}
        return negatives_from_unconsumed(
            user_consumed_set, users, items, n_items, num_neg
        )

    def __len__(self):
        return len(self.labels)

    def __getitem__(self, index):
        """Get a slice of data."""
        return self.user_indices[index], self.item_indices[index], self.labels[index]
