#!/usr/bin/python
"""Utilities to facilitate out-of-core learning in sklearn"""

import collections
import numpy as np
import scipy

import sklearn.base


class Streamer(object):
    '''A wrapper class for reusable generators.

    :usage:
        >>> # make a generator
        >>> def my_generator(n):
                for i in range(n):
                    yield i
        >>> GS = Streamer(my_generator, 5)
        >>> for i in GS.generate():
                print i

        >>> # Or with a maximum number of items
        >>> for i in GS.generate(max_items=3):
                print i

    :parameters:
        - streamer : function or iterable
          Any generator function or iterable python object

        - *args, **kwargs
          Additional positional arguments or keyword arguments to pass
          through to ``generator()``

    :raises:
        - TypeError
          If ``streamer`` is not a function or an Iterable object.
    '''

    def __init__(self, streamer, *args, **kwargs):
        '''Initializer'''

        if not (hasattr(streamer, '__call__') or
                isinstance(streamer, collections.Iterable)):
            raise TypeError('streamer must be a generator or Iterable')

        self.stream = streamer
        self.args = args
        self.kwargs = kwargs

    def generate(self, max_items=None):
        '''Instantiate the generator

        :parameters:
            - max_items : None or int > 0
              Maximum number of items to yield.
              If ``None``, exhaust the generator.

        :yields:
            - Items from the contained generator
        '''

        if max_items is None:
            max_items = np.inf

        # If it's a function, create the stream.
        # If it's iterable, use it directly.

        if hasattr(self.stream, '__call__'):
            my_stream = self.stream(*(self.args), **(self.kwargs))

        else:
            my_stream = self.stream

        for i, x in enumerate(my_stream):
            if i >= max_items:
                break
            yield x


def _buffer_data(data):
    """Determine whether the data is sparse or not, and buffer it accordingly.

    :parameters:
        - data : list of scipy.sparse or np.ndarray
            The data to buffer

    :returns:
        - buf : scipy.sparse.csr or np.ndarray
            If the input data was sparse, a sparse matrix of the data
            concatenated vertically.
            Otherwise, the data stacked vertically as a dense ndarray.
    """

    if scipy.sparse.issparse(data[0]):
        n = len(data)
        dimension = np.prod(data[0].shape)

        data_s = scipy.sparse.lil_matrix((n, dimension), dtype=data[0].dtype)

        for i in range(len(data)):
            idx = data[i].indices
            data_s[i, idx] = data[i][:, idx]

        return data_s.tocsr()

    else:
        return np.asarray(data)


def categorical_sample(weights):
    '''Sample from a categorical distribution.

    :parameters:
        - weights : np.array, shape=(n,)
          The distribution to sample from.
          Must be non-negative and sum to 1.0.

    :returns:
        - k : int in [0, n)
          The sample
    '''

    return np.flatnonzero(np.random.multinomial(1, weights))[0]


def mux(seed_pool, n_samples, k, lam=256.0, pool_weights=None,
        with_replacement=True):
    '''Stochastic multiplexor for generator seeds.

    Given an array of Streamer objects, do the following:

        1. Select ``k`` seeds at random to activate
        2. Assign each activated seed a sample count ~ Poisson(lam)
        3. Yield samples from the streams by randomly multiplexing
           from the active set.
        4. When a stream is exhausted, select a new one from the pool.

    :parameters:
        - seed_pool : iterable of Streamer
          The collection of Streamer objects

        - n_samples : int > 0 or None
          The number of samples to generate.
          If ``None``, sample indefinitely.

        - k : int > 0
          The number of streams to keep active at any time.

        - lam : float > 0 or None
          Rate parameter for the Poisson distribution governing sample counts
          for individual streams.
          If ``None``, sample infinitely from each stream.

        - pool_weights : np.ndarray or None
          Optional weighting for ``seed_pool``.
          If ``None``, then weights are assumed to be uniform.
          Otherwise, ``pool_weights[i]`` defines the sampling proportion
          of ``seed_pool[i]``.

          Must have the same length as ``seed_pool``.

        - with_replacement : bool
          Sample Streamers with replacement.  This allows a single stream to be
          used multiple times (even simultaneously).
          If ``False``, then each Streamer is consumed at most once and never
          revisited.

    '''
    n_seeds = len(seed_pool)

    # Set up the sampling distribution over streams
    seed_distribution = 1./n_seeds * np.ones(n_seeds)

    if pool_weights is None:
        pool_weights = seed_distribution.copy()

    assert len(pool_weights) == len(seed_pool)
    assert (pool_weights > 0.0).all()
    pool_weights /= np.sum(pool_weights)

    # Instantiate the pool
    streams = [None] * k

    stream_weights = np.zeros(k)

    for idx in range(k):

        if not (seed_distribution > 0).any():
            break

        # how many samples for this stream?
        # pick a stream
        new_idx = categorical_sample(seed_distribution)

        # instantiate
        if lam is not None:
            n_stream = np.random.poisson(lam=lam)
        else:
            n_stream = None

        streams[idx] = seed_pool[new_idx].generate(max_items=n_stream)
        stream_weights[idx] = pool_weights[new_idx]

        # If we're sampling without replacement, zero this one out
        if not with_replacement:
            seed_distribution[new_idx] = 0.0

            if (seed_distribution > 0).any():
                seed_distribution[:] /= np.sum(seed_distribution)

    weight_norm = np.sum(stream_weights)

    # Main sampling loop
    n = 0

    if n_samples is None:
        n_samples = np.inf

    while n < n_samples and weight_norm > 0.0:
        # Pick a stream
        idx = categorical_sample(stream_weights / weight_norm)

        # Can we sample from it?
        try:
            # Then yield the sample
            yield streams[idx].next()

            # Increment the sample counter
            n = n + 1

        except StopIteration:
            # Oops, this one's exhausted.  Replace it and move on.

            # Are there still kids in the pool?  Okay.
            if (seed_distribution > 0).any():

                new_idx = categorical_sample(pool_weights)

                if lam is not None:
                    n_stream = np.random.poisson(lam=lam)
                else:
                    n_stream = None

                streams[idx] = seed_pool[new_idx].generate(max_items=n_stream)

                stream_weights[idx] = pool_weights[new_idx]

                # If we're sampling without replacement, zero out this one out
                if not with_replacement:
                    seed_distribution[new_idx] = 0.0

                    if (seed_distribution > 0).any():
                        seed_distribution[:] /= np.sum(seed_distribution)

            else:
                # Otherwise, this one's exhausted.  Set its probability to 0
                stream_weights[idx] = 0.0

            weight_norm = np.sum(stream_weights)


# TODO:   2014-07-10 16:38:27 by Brian McFee <brm2132@columbia.edu>
# make existing functions wrap transparently (fit, predict, etc)
# implement iterable versions on top: iter_fit, iter_predict, etc

class StreamLearner(sklearn.base.BaseEstimator):
    '''A class to facilitate iterative learning from a generator.

    :parameters:
        - estimator : sklearn estimator
            The estimator to fit.  Must support the ``partial_fit`` method.

        - batch_size : int > 0
            The size of batches to be passed to ``estimator.partial_fit``.

        - max_steps : None or int > 0
            Maximum number of batch learning iterations.
            If ``None``, the learner runs until the input stream is exhausted.
    '''

    def __init__(self, estimator, batch_size=100, max_steps=None):
        ''' '''
        # Is this a supervised or unsupervised learner?
        self.supervised = isinstance(estimator, sklearn.base.ClassifierMixin)

        # Does the learner support partial fit?
        assert hasattr(estimator, 'partial_fit')

        # Is the batch size positive?
        assert batch_size > 0

        # Is the iteration bound positive or infinite?
        if max_steps is not None:
            assert max_steps > 0

        self.estimator = estimator
        self.batch_size = int(batch_size)
        self.max_steps = max_steps

    def __partial_fit(self, data, **kwargs):
        """Wrapper function to estimator.partial_fit()"""

        if self.supervised:
            args = [_buffer_data(datum) for datum in zip(*data)]
        else:
            args = [_buffer_data(data)]

        self.estimator.partial_fit(*args, **kwargs)

    def decision_function(self, *args, **kwargs):
        '''Wrapper for estimator.predict()'''

        return self.estimator.decision_function(*args, **kwargs)

    def predict_proba(self, *args, **kwargs):
        '''Wrapper for estimator.predict_proba()'''

        return self.estimator.predict_proba(*args, **kwargs)

    def predict_log_proba(self, *args, **kwargs):
        '''Wrapper for estimator.predict_log_proba()'''

        return self.estimator.predict(*args, **kwargs)

    def predict(self, *args, **kwargs):
        '''Wrapper for estimator.predict()'''

        return self.estimator.predict(*args, **kwargs)

    def inverse_transform(self, *args, **kwargs):
        '''Wrapper for estimator.inverse_transform()'''

        return self.estimator.inverse_transform(*args, **kwargs)

    def transform(self, *args, **kwargs):
        '''Wrapper for estimator.transform()'''

        return self.estimator.transform(*args, **kwargs)

    def fit_transform(self, *args, **kwargs):
        '''Wrapper for estimator.fit_transform()'''

        return self.estimator.fit_transform(*args, **kwargs)

    def score(self, *args, **kwargs):
        '''Wrapper for estimator.score()'''

        return self.estimator.score(*args, **kwargs)

    def fit(self, *args, **kwargs):
        '''Wrapper for estimator.fit()'''

        return self.estimator.fit(*args, **kwargs)

    def iter_fit(self, stream, **kwargs):
        '''Iterative learning.

        :parameters:
            - stream : iterable of (x) or (x, y)
              The data stream to fit.  Each element is assumed to be a
              single example, or a tuple of (example, label).

              Examples are collected into a batch and passed to
              ``estimator.partial_fit``.

            - kwargs
              Additional keyword arguments to ``estimator.partial_fit``.
              This is useful for things like the list of class labels for an
              SGDClassifier.

        :returns:
            - self
        '''

        # Re-initialize the model, if necessary?

        buf = []
        for i, x_new in enumerate(stream):
            buf.append(x_new)

            # We've run too far, stop
            if self.max_steps is not None and i > self.max_steps:
                break

            # Buffer is full, do an update
            if len(buf) == self.batch_size:
                self.__partial_fit(buf, **kwargs)
                buf = []

        # Update on whatever's left over
        if len(buf) > 0:
            self.__partial_fit(buf, **kwargs)

        return self