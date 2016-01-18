# -*- coding: utf-8 -*-
"""
Fuzzy pattern classifier with negative and positive examples


References:
-----------
[1] Davidsen 2015.

"""

import logging
import numpy as np
from sklearn.base import BaseEstimator, ClassifierMixin
from sklearn.utils.validation import check_array
from sklearn.metrics import mean_squared_error
from sklearn.neighbors import DistanceMetric
from fuzzylogic import PiSet, TriangularSet, owa, meowa, p_normalize, prod
from ga import UnitIntervalGeneticAlgorithm, helper_fitness, helper_n_generations, UniformCrossover
from local_search import PatternSearchOptimizer, helper_num_runs, LocalUnimodalSamplingOptimizer

logger = logging.getLogger(__name__)

def pi_factory(**kwargs):
    m = kwargs["m"] if "m" in kwargs else 2.0
    c = kwargs["mean"]
    d = (kwargs["max"] - kwargs["min"]) / 2.0
    return PiSet(a=c - d, r=c, b=c + d, m=m)

def t_factory(**kwargs):
    c = kwargs["mean"]
    d = (kwargs["max"] - kwargs["min"]) / 2.0
    return TriangularSet(c - d, c, c + d)

def distancemetric_f(name, **kwargs):
    """
    Factory for a distance metric supported by DistanceMetric
    """
    def _distancemetric_factory(X):
        return DistanceMetric.get_metric(name)
    return _distancemetric_factory

def build_memberships(X, class_idx, factory):
    # take column-wise min/mean/max for class
    mins = np.nanmin(X[class_idx], 0)
    means = np.nanmean(X[class_idx], 0)
    maxs = np.nanmax(X[class_idx], 0)
    return [ factory(min=mins[i], mean=means[i], max=maxs[i]) for i in range(X.shape[1]) ]

#
# Authors: Søren Atmakuri Davidsen <sorend@gmail.com>
#

def predict_proto(X, proto, aggregation, A):
    for col_no in range(X.shape[1]):
        A[:, col_no] = proto[col_no](X[:, col_no])
    return aggregation(A, axis=1)

def predict_protos(X, protos, aggregation):
    y = np.zeros((X.shape[0], len(protos)))
    A = np.zeros(X.shape)  # re-use this matrix
    for clz_no, proto in enumerate(protos):
        y[:, clz_no] = predict_proto(X, proto, aggregation, A)
    return y

class IterativeShrinking:
    def __init__(self, iterations=3, alpha_cut=0.1):
        self.iterations = iterations
        self.alpha_cut = alpha_cut

    def __call__(self, X, class_idx, mu_factory, aggregation):
        return [ self.shrink_for_feature(X[class_idx, i], mu_factory, self.alpha_cut, self.iterations)
                 for i in range(X.shape[1]) ]

    def shrink_for_feature(self, C, factory, alpha_cut, iterations):
        """
        Performs shrinking for a single dimension (feature)
        """
        def create_mu(idx):
            s = sum(idx) > 0
            tmin = np.nanmin(C[idx]) if s else 0
            tmax = np.nanmax(C[idx]) if s else 1
            tmean = np.nanmean(C[idx]) if s else 0.5
            return factory(min=tmin, mean=tmean, max=tmax)

        C_idx = C >= 0  # create mask
        mu = create_mu(C_idx)
        # mu_orig = mu

        for iteration in range(iterations):
            A = mu(C)
            C_idx = A >= alpha_cut
            mu = create_mu(C_idx)

        return mu

class GAShrinking:
    """
    A shrinking method using GA.
    """
    def __init__(self, iterations=25, adjust_center=True, adjust_symmetric=True):
        """
        Constructor

        Parameters:
        -----------

        iterations : number of iterations to use for the GA.

        adjust_center : allow the GA to adjust center of set.

        adjust_symmetric : symmetric adjustment of set.
        """
        self.iterations = iterations
        self.adjust_center = adjust_center
        self.adjust_symmetric = adjust_symmetric

    def __call__(self, X, class_idx, mu_factory, aggregation):

        # take column-wise min/mean/max for class
        mins = np.nanmin(X[class_idx], 0)
        means = np.nanmean(X[class_idx], 0)
        maxs = np.nanmax(X[class_idx], 0)
        ds = (maxs - mins) / 2.0

        m = X.shape[1]
        n_genes = 2 * m if self.adjust_symmetric else 3 * m

        def decode_with_shrinking_expanding(C):
            def dcenter(j):
                return C[j * 2] - 0.5 if self.adjust_center else 0.0

            def left(j):
                return ds[j] * C[(j * 2) + 1]

            def right(j):
                return ds[j] * C[(j * 2) + 1] if self.adjust_symmetric else ds[j] * C[(j * 2) + 2]

            return [ PiSet(r=means[j] + dcenter(j),
                           p=means[j] - left(j),
                           q=means[j] + right(j)) for j in range(m) ]

        y_target = np.zeros(X.shape[0])  # create the target of 1 and 0.
        y_target[class_idx] = 1.0

        A = np.zeros(X.shape)  # use for calculating memberships values.

        def rmse_fitness_function(chromosome):
            proto = decode_with_shrinking_expanding(chromosome)
            y_pred = predict_proto(X, proto, aggregation, A)
            return mean_squared_error(y_target, y_pred)

        logger.info("initializing GA %d iterations" % (self.iterations,))
        # initialize
        ga = UnitIntervalGeneticAlgorithm(fitness_function=helper_fitness(rmse_fitness_function),
                                          crossover_function=UniformCrossover(0.5),
                                          elitism=3,
                                          n_chromosomes=100,
                                          n_genes=n_genes,
                                          p_mutation=0.3)

        ga = helper_n_generations(ga, self.iterations)
        chromosomes, fitnesses = ga.best(1)

        return decode_with_shrinking_expanding(chromosomes[0])

class ShrinkingFuzzyPatternClassifier(BaseEstimator, ClassifierMixin):

    def __init__(self, aggregation=np.mean,
                 membership_factory=pi_factory,
                 shrinking=IterativeShrinking(iterations=3, alpha_cut=0.05),
                 arg_select=np.nanargmax,
                 **kwargs):
        """
        Constructs classifier

        Params:
        -------
        aggregation : Aggregation to use in the classifier.

        membership_factory : factory to construct membership functions.

        shrinking : function to use for shrinking.

        arg_select : function to select class from aggregated mu values.

        """
        self.aggregation = aggregation
        self.membership_factory = membership_factory
        self.shrinking = shrinking
        self.arg_select = arg_select

    def get_params(self, deep=False):
        return {"aggregation": self.aggregation,
                "membership_factory": self.membership_factory,
                "shrinking": self.shrinking,
                "arg_select": self.arg_select}

    def set_params(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)
        return self

    def fit(self, X, y):

        X = check_array(X)

        self.classes_, y = np.unique(y, return_inverse=True)

        if np.nan in self.classes_:
            raise ValueError("nan not supported for class values")

        # build membership functions for each feature for each class
        self.protos_ = [
            self.shrinking(X, y == idx, self.membership_factory, self.aggregation)
            for idx, class_value in enumerate(self.classes_)
        ]

        return self

    def predict(self, X):
        """
        Predicts if examples in X belong to classifier's class or not.

        Parameters
        ----------
        X : examples to predict for.
        """
        if not hasattr(self, "protos_"):
            raise Exception("Perform a fit first.")

        y_mu = predict_protos(X, self.protos_, self.aggregation)

        return self.classes_.take(self.arg_select(y_mu, 1))

    def predict_proba(self, X):
        if not hasattr(self, "protos_"):
            raise Exception("Perform a fit first.")

        X = check_array(X)

        if X.shape[1] != len(self.mus_):
            raise ValueError("Number of features do not match trained number of features")

        y_mu = predict_protos(X, self.protos_, self.aggregation)

        return p_normalize(y_mu, 1)

class ga_owa_optimizer(object):

    def __init__(self, f_evals=10):
        self.f_evals = f_evals

    def __call__(self, X, fitness):
        iterations = X.shape[1] * self.f_evals
        ga = UnitIntervalGeneticAlgorithm(fitness_function=helper_fitness(fitness),
                                          n_chromosomes=50,
                                          elitism=3,
                                          p_mutation=0.1,
                                          n_genes=X.shape[1])
        ga = helper_n_generations(ga, iterations)
        chromosomes, fitnesses = ga.best(1)
        return chromosomes[0]

    def __str__(self):
        return "ga"

class pslus_owa_optimizer(object):

    def __init__(self, name, cls, f_evals):
        self.name = name
        self.cls = cls
        self.f_evals = f_evals

    def __call__(self, X, fitness):
        lower_bounds = np.array([0.0] * X.shape[1])
        upper_bounds = np.array([1.0] * X.shape[1])
        max_evaluations = X.shape[1] * self.f_evals
        ps = self.cls(fitness, lower_bounds, upper_bounds, max_evaluations=max_evaluations)
        best_sol, best_fit = helper_num_runs(ps, num_runs=10)
        return best_sol

    def __str__(self):
        return self.name

def ps_owa_optimizer(f_evals=5):
    return pslus_owa_optimizer("ps", PatternSearchOptimizer, f_evals)

def lus_owa_optimizer(f_evals=10):
    return pslus_owa_optimizer("lus", LocalUnimodalSamplingOptimizer, f_evals)

def build_y_target(y, classes):
    y_target = np.zeros((len(y), len(classes)))
    for i, c in enumerate(classes):
        y_target[y == i, i] = 1.0
    return y_target

class GAOWAFactory:

    def __init__(self, optimizer=ga_owa_optimizer()):
        self.optimizer = optimizer

    def __call__(self, protos, X, y, classes):

        y_target = build_y_target(y, classes)

        def fitness(c):
            aggr = owa(p_normalize(c))
            y_pred = predict_protos(X, protos, aggr)
            if np.isnan(np.sum(y_pred)):  # quick check if any nan in results.
                return 1.0
            else:
                return mean_squared_error(y_target, y_pred)

        weights = self.optimizer(X, fitness)

        weights = p_normalize(weights)

        logger.info("trained owa(%s, %s)" % (str(self.optimizer),
                                             ", ".join(map(lambda x: "%.5f" % (x,), weights))))

        return owa(weights)

class StaticFactory:

    def __init__(self, aggregation=prod):
        self.aggregation = aggregation

    def __call__(self, *args, **kwargs):
        return self.aggregation

class MEOWAFactory:

    def __call__(self, protos, X, y, classes):

        y_target = build_y_target(y, classes)

        best, best_mse = None, 1.0
        for p in np.linspace(0, 1, 11):
            aggr = meowa(X.shape[1], p)
            y_pred = predict_protos(X, protos, aggr)
            mse = mean_squared_error(y_target, y_pred)
            if mse < best_mse:
                best = aggr
                best_mse = mse

        logger.info("trained owa(meowa, %s)" % (", ".join(map(lambda x: "%.5f" % (x,), best.v))))

        return best

class OWAFuzzyPatternClassifier(BaseEstimator, ClassifierMixin):
    """
    Fuzzy pattern classifier using OWA as aggregation with weights searched by a GA.
    """
    def get_params(self, deep=False):
        return {"aggregation_factory": self.aggregation_factory,
                "membership_factory": self.membership_factory}

    def set_params(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)
        return self

    def __init__(self, membership_factory=pi_factory, aggregation_factory=GAOWAFactory()):
        self.aggregation_factory = aggregation_factory
        self.membership_factory = membership_factory

    def fit(self, X, y):

        X = check_array(X)

        self.classes_, y = np.unique(y, return_inverse=True)

        if "?" in tuple(self.classes_):
            raise ValueError("nan not supported for class values")

        # build membership functions for each feature for each class
        self.protos_ = [
            build_memberships(X, y == idx, self.membership_factory)
            for idx, class_value in enumerate(self.classes_)
        ]

        # build aggregation
        self.aggregation_ = self.aggregation_factory(self.protos_, X, y, self.classes_)

        return self

    def predict(self, X):
        """
        Predicts if examples in X belong to classifier's class or not.

        Parameters
        ----------
        X : examples to predict for.
        """
        if not hasattr(self, "classes_"):
            raise Exception("Perform a fit first.")

        y_mu = predict_protos(X, self.protos_, self.aggregation_)

        return self.classes_.take(np.argmax(y_mu, 1))

    def predict_proba(self, X):

        if not hasattr(self, "classes_"):
            raise Exception("Perform a fit first.")

        X = check_array(X)

        y_mu = predict_protos(X, self.protos_, self.aggregation_)

        return p_normalize(y_mu, 1)  # constrain membership values to probability sum(row) = 1