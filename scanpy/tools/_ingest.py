import pandas as pd
import numpy as np
from sklearn.utils import check_random_state
from scipy.sparse import issparse
from anndata.core.anndata import AxisArrays

from ..preprocessing._simple import N_PCS
from ..neighbors import _rp_forest_generate


def ingest(
    adata,
    adata_ref,
    obs=None,
    inplace=True,
    embedding_method=('umap', 'pca'),
    labeling_method='knn',
    return_joint=False,
    batch_key='batch',
    batch_categories=None,
    index_unique='-',
    **kwargs,
):
    """\
    Map labels and embeddings from existing data to new data.

    This function allows to map the specified labels and embeddings
    from `adata_ref` to `adata`.
    The function uses the k-nearest neighbors method for mapping labels.
    You need to run :func:`~scanpy.pp.neighbors` on `adata_ref` before
    passing it.

    Parameters
    ----------
    adata : :class:`~anndata.AnnData`
        The annotated data matrix of shape `n_obs` × `n_vars`. Rows correspond
        to cells and columns to genes. This is the dataset without labels and
        embeddings.
    adata_ref : :class:`~anndata.AnnData`
        The annotated data matrix of shape `n_obs` × `n_vars`. Rows correspond
        to cells and columns to genes.
        Variables (`n_vars` and `var_names`) of `adata_ref` should be the same
        as in `adata`.
        This is the dataset with labels and embeddings
        which need to be mapped to `adata`.
    obs : `str` or list of `str` or `None`, optional (default: `None`)
        Labels' keys in `adata_ref.obs` which need to be mapped to `adata.obs`
        (inferred for observation of `adata`).
    embedding_method : `str` or list of `str`, optional (default: `('umap', 'pca')`)
        Embeddings in `adata_ref` which need to be mapped to `adata`.
        The only supported values are 'umap' and 'pca'.
    labeling_method : `str`, optional (default: `knn`)
        The method to map labels in `adata_ref.obs` to `adata.obs`.
        The only supported value is 'knn'.
    return_joint : `bool`, optional (default: `False`)
        If set to `True` the function
        returns the new :class:`~anndata.AnnData` object with concatenated
        existing embeddings and labels of 'adata_ref' and inferred embeddings
        and labels for `adata`.
    batch_key : `str`, optional (default: `'batch'`)
        Only works if `return_joint=True`.
        Add the batch annotation to `obs`
        of the new :class:`~anndata.AnnData` object using this key.
    batch_categories : `str` or `None`, optional (default: `None`)
        Only works if `return_joint=True`.
        Use these as categories for the batch annotation.
        By default, use increasing numbers.
    index_unique : `str` or `None`, optional (default: `None`)
        Only works if `return_joint=True`.
        Make the index unique by joining the existing index names with the
        batch category, using `index_unique='-'`, for instance. Provide
        `None` to keep existing indices.
    inplace : `bool`, optional (default: `True`)
        Only works if `return_joint=False`.
        Add labels and embeddings to the passed `adata` (if `True`)
        or return a copy of `adata` with mapped embeddings and labels.

    Returns
    -------
    if `return_joint=True` returns the new :class:`~anndata.AnnData` object
    with concatenated existing embeddings and labels of 'adata_ref' and
    inferred embeddings and labels for `adata`.
    if `return_joint=False` then:

    * if `inplace=False` returns a copy of `adata`
      with mapped embeddings and labels in `obsm` and `obs` correspondingly.
    * if `inplace=True` returns nothing and updates `adata.obsm` and `adata.obs`
      with mapped embeddings and labels.

    Example
    -------
    Assuming there is `adata_ref` with 'cell_type' in `adata_ref.obs`
    which we want to infer for observations in `adata`.
    >>> sc.pp.neighbors(adata_ref)
    >>> sc.tl.umap(adata_ref)
    >>> adata_joint = sc.tl.ingest(adata, adata_ref, obs='cell_type',
                                   embedding_method='umap',
                                   batch_key='ing_batch',
                                   return_joint=True)
    """
    obs = [obs] if isinstance(obs, str) else obs
    embedding_method = (
        [embedding_method]
        if isinstance(embedding_method, str)
        else embedding_method
    )
    labeling_method = (
        [labeling_method]
        if isinstance(labeling_method, str)
        else labeling_method
    )

    if len(labeling_method) == 1 and len(obs or []) > 1:
        labeling_method = labeling_method * len(obs)

    ing = Ingest(adata_ref)
    ing.fit(adata)

    if embedding_method is not None:
        for method in embedding_method:
            ing.map_embedding(method)

    if obs is not None:
        ing.neighbors(**kwargs)
        for i, col in enumerate(obs):
            ing.map_labels(col, labeling_method[i])

    if return_joint:
        return ing.to_adata_joint(batch_key, batch_categories, index_unique)
    else:
        return ing.to_adata(inplace)


class Ingest:
    """\
    Class to map labels and embeddings from existing data to new data.

    You need to run :func:`~scanpy.pp.neighbors` on `adata` before
    initializing Ingest with it.

    Parameters
    ----------
    adata : :class:`~anndata.AnnData`
        The annotated data matrix of shape `n_obs` × `n_vars`
        with embeddings and labels.
    """

    def _init_umap(self, adata):
        from umap import UMAP

        self._umap = UMAP(metric=adata.uns['neighbors']['params']['metric'])

        self._umap.embedding_ = adata.obsm['X_umap']
        self._umap._raw_data = self._rep
        self._umap._sparse_data = issparse(self._rep)
        self._umap._small_data = self._rep.shape[0] < 4096
        self._umap._metric_kwds = adata.uns['neighbors']['params'].get(
            'metric_kwds', {}
        )
        self._umap._n_neighbors = adata.uns['neighbors']['params'][
            'n_neighbors'
        ]
        self._umap._initial_alpha = self._umap.learning_rate

        self._umap._random_init = self._random_init
        self._umap._tree_init = self._tree_init
        self._umap._search = self._search

        self._umap._rp_forest = self._rp_forest

        self._umap._search_graph = self._search_graph

        self._umap._a = adata.uns['umap']['params']['a']
        self._umap._b = adata.uns['umap']['params']['b']

        self._umap._input_hash = None

    def _init_neighbors(self, adata):
        from umap.distances import named_distances
        from umap.nndescent import (
            make_initialisations,
            make_initialized_nnd_search,
        )

        if 'use_rep' in adata.uns['neighbors']['params']:
            self._use_rep = adata.uns['neighbors']['params']['use_rep']
            self._rep = (
                adata.X if self._use_rep == 'X' else adata.obsm[self._use_rep]
            )
        elif 'n_pcs' in adata.uns['neighbors']['params']:
            self._use_rep = 'X_pca'
            self._n_pcs = adata.uns['neighbors']['params']['n_pcs']
            self._rep = adata.obsm['X_pca'][:, : self._n_pcs]
        elif adata.n_vars > N_PCS and 'X_pca' in adata.obsm.keys():
            self._use_rep = 'X_pca'
            self._rep = adata.obsm['X_pca'][:, :N_PCS]
            self._n_pcs = self._rep.shape[1]

        if 'metric_kwds' in adata.uns['neighbors']['params']:
            dist_args = tuple(
                adata.uns['neighbors']['params']['metric_kwds'].values()
            )
        else:
            dist_args = ()
        dist_func = named_distances[adata.uns['neighbors']['params']['metric']]
        self._random_init, self._tree_init = make_initialisations(
            dist_func, dist_args
        )
        self._search = make_initialized_nnd_search(dist_func, dist_args)

        search_graph = adata.uns['neighbors']['distances'].copy()
        search_graph.data = (search_graph.data > 0).astype(np.int8)
        self._search_graph = search_graph.maximum(search_graph.transpose())

        if 'rp_forest' in adata.uns['neighbors']:
            self._rp_forest = _rp_forest_generate(
                adata.uns['neighbors']['rp_forest']
            )
        else:
            self._rp_forest = None

    def _init_pca(self, adata):
        self._pca_centered = adata.uns['pca']['params']['zero_center']
        self._pca_use_hvg = adata.uns['pca']['params']['use_highly_variable']

        if self._pca_use_hvg and 'highly_variable' not in adata.var.keys():
            raise ValueError('Did not find adata.var[\'highly_variable\'].')

        if self._pca_use_hvg:
            self._pca_basis = adata.varm['PCs'][adata.var['highly_variable']]
        else:
            self._pca_basis = adata.varm['PCs']

    def __init__(self, adata):
        # assume rep is X if all initializations fail to identify it
        self._rep = adata.X
        self._use_rep = 'X'

        self._n_pcs = None

        self._adata_ref = adata
        self._adata_new = None

        if 'pca' in adata.uns:
            self._init_pca(adata)

        if 'neighbors' in adata.uns:
            self._init_neighbors(adata)

        if 'X_umap' in adata.obsm:
            self._init_umap(adata)

        self._obsm = None
        self._obs = None
        self._labels = None

        self._indices = None
        self._distances = None

    def _pca(self, n_pcs=None):
        X = self._adata_new.X
        X = X.toarray() if issparse(X) else X.copy()
        if self._pca_use_hvg:
            X = X[:, self._adata_ref.var['highly_variable']]
        if self._pca_centered:
            X -= X.mean(axis=0)
        X_pca = np.dot(X, self._pca_basis[:, :n_pcs])
        return X_pca

    def _same_rep(self):
        adata = self._adata_new
        if self._n_pcs is not None:
            return self._pca(self._n_pcs)
        if self._use_rep == 'X':
            return adata.X
        if self._use_rep in adata.obsm.keys():
            return adata.obsm[self._use_rep]
        return adata.X

    def fit(self, adata_new):
        """\
        Map `adata_new` to the same representation as `adata`.

        This function identifies the representation which was used to
        calculate neighbors in 'adata' and maps `adata_new` to
        this representation.
        Variables (`n_vars` and `var_names`) of `adata_new` should be the same
        as in `adata`.

        `adata` refers to the :class:`~anndata.AnnData` object
        that is passed during the initialization of an Ingest instance.
        """
        ref_var_names = self._adata_ref.var_names.str.upper()
        new_var_names = adata_new.var_names.str.upper()

        if not ref_var_names.equals(new_var_names):
            raise ValueError(
                'Variables in the new adata are different '
                'from variables in the reference adata'
            )

        self._obs = pd.DataFrame(index=adata_new.obs.index)
        # not sure if it should be AxisArrays
        self._obsm = AxisArrays(adata_new, 0)

        self._adata_new = adata_new
        self._obsm['rep'] = self._same_rep()

    def neighbors(self, k=10, queue_size=5, random_state=0):
        """\
        Calculate neighbors of `adata_new` observations in `adata`.

        This function calculates `k` neighbors in `adata` for
        each observation of `adata_new`.
        """
        from umap.nndescent import initialise_search
        from umap.utils import deheap_sort
        from umap.umap_ import INT32_MAX, INT32_MIN

        random_state = check_random_state(random_state)
        rng_state = random_state.randint(INT32_MIN, INT32_MAX, 3).astype(
            np.int64
        )

        train = self._rep
        test = self._obsm['rep']

        init = initialise_search(
            self._rp_forest,
            train,
            test,
            int(k * queue_size),
            self._random_init,
            self._tree_init,
            rng_state,
        )

        result = self._search(
            train,
            self._search_graph.indptr,
            self._search_graph.indices,
            init,
            test,
        )
        indices, dists = deheap_sort(result)
        self._indices, self._distances = indices[:, :k], dists[:, :k]

    def _umap_transform(self):
        return self._umap.transform(self._obsm['rep'])

    def map_embedding(self, method):
        """\
        Map embeddings of `adata` to `adata_new`.

        This function infers embeddings, specified by `method`,
        for `adata_new` from existing embeddings in `adata`.
        `method` can be 'umap' or 'pca'.
        """
        if method == 'umap':
            self._obsm['X_umap'] = self._umap_transform()
        elif method == 'pca':
            self._obsm['X_pca'] = self._pca()
        else:
            raise NotImplementedError(
                'Ingest supports only umap and pca embeddings for now.'
            )

    def _knn_classify(self, labels):
        cat_array = self._adata_ref.obs[labels]

        values = [cat_array[inds].mode()[0] for inds in self._indices]
        return pd.Categorical(
            values=values, categories=cat_array.cat.categories
        )

    def map_labels(self, labels, method):
        """\
        Map labels of `adata` to `adata_new`.

        This function infers `labels` for `adata_new.obs`
        from existing labels in `adata.obs`.
        `method` can be only 'knn'.
        """
        if method == 'knn':
            self._obs[labels] = self._knn_classify(labels)
        else:
            raise NotImplementedError('Ingest supports knn labeling for now.')

    def to_adata(self, inplace=False):
        """\
        Returns `adata_new` with mapped embeddings and labels.

        If `inplace=False` returns a copy of `adata_new`
        with mapped embeddings and labels in `obsm` and `obs` correspondingly.
        If `inplace=True` returns nothing and updates `adata_new.obsm`
        and `adata_new.obs` with mapped embeddings and labels.
        """
        adata = self._adata_new if inplace else self._adata_new.copy()

        adata.obsm.update(self._obsm)

        for key in self._obs:
            adata.obs[key] = self._obs[key]

        if not inplace:
            return adata

    def to_adata_joint(
        self, batch_key='batch', batch_categories=None, index_unique='-'
    ):
        """\
        Returns concatenated object.

        This function returns the new :class:`~anndata.AnnData` object
        with concatenated existing embeddings and labels of 'adata'
        and inferred embeddings and labels for `adata_new`.
        """
        adata = self._adata_ref.concatenate(
            self._adata_new,
            batch_key=batch_key,
            batch_categories=batch_categories,
            index_unique=index_unique,
        )

        obs_update = self._obs.copy()
        obs_update.index = adata[adata.obs[batch_key] == '1'].obs_names
        adata.obs.update(obs_update)

        for key in self._obsm:
            if key in self._adata_ref.obsm:
                adata.obsm[key] = np.vstack(
                    (self._adata_ref.obsm[key], self._obsm[key])
                )

        if self._use_rep not in ('X_pca', 'X'):
            adata.obsm[self._use_rep] = np.vstack(
                (self._adata_ref.obsm[self._use_rep], self._obsm['rep'])
            )

        if 'X_umap' in self._obsm:
            adata.uns['umap'] = self._adata_ref.uns['umap']
        if 'X_pca' in self._obsm:
            adata.uns['pca'] = self._adata_ref.uns['pca']
            adata.varm['PCs'] = self._adata_ref.varm['PCs']

        return adata
