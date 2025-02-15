#!/usr/bin/python
# -*- coding: utf-8 -*-
# Author: Andreas Buttenschoen
from pylops import LinearOperator
import numpy as np
import scipy.sparse.linalg as LAS
import scipy.linalg as LA

from fun import Fun, h1norm, norm, norm2
from support.Functional import Functional
from cheb.chebpts import quadwts
from cheb.diff import computeDerCoeffs


class DeflatedResidual(LinearOperator):
    r"""
        This generates the operator representing the derivative of a deflated
        Newton's method at the point u.

        So during construction we evaluate the map u -> D_u G[u]

        Given a nonlinear root-finding problem:  F(a, u) = 0
        and a set of known solutions { u_k }_{k=1}^{N}

        the deflated residual is given by:

                       N
            G(a, u) =  π    (sigma + 1 / || u - u_k || )  F(a, u)
                      k = 1

        where the norm is appropriately chosen for the problem at hand.
    """
    def __init__(self, u, operator, par=False, dtype=None, *args, **kwargs):
        """ Arguments:

            u:  Function; location of linearization.
            us: Nonlinear operator source -> carries out linearization
        """
        # main sparse matrix -> need to update coefficients here!
        self.colloc = operator.discretize(u, par=par)

        # Create matrices
        self.M, self.P, self.S = self.colloc.matrix()
        self.proj = self.colloc.P
        self.b = self.colloc.rhs()
        #print('M = ', self.M.shape)

        # make sure shapes are correct
        if np.product(u.shape) != self.M.shape[0]:
            u.prolong(operator.n_disc)
        # TODO: Can the opposite case occur?

        # this must now hold
        # assert np.product(u.shape) == self.M.shape[0], 'Shape mismatch between linearised operator and function!'

        # store the transpose
        self.MT = None

        self.shape = self.M.shape
        self.dtype = np.dtype(dtype)
        self.explicit = False

        # known solutions
        self.known_solutions = kwargs.get('ks', [])

        # deflation parameters
        self.shift = kwargs.get('shift', 1.0)
        self.power = kwargs.get('power', 2)

        # need to setup! TODO: fix the u.u here!
        try:
            self.eta, self.functional = self.__compute_der_deflation(u.u)
        except AttributeError:
            try:
                self.eta, self.functional = self.__compute_der_deflation(u)
            except Exception as e:
                raise e

        # TODO: improve this!
        #self.prcond = self.precond()

        # update the right hand side with the deflation number
        if self.known_solutions:
            self.b = self.b * self.eta

    def rhs(self, u):
        """ Recomputes the residual at a new state u """
        res = self.colloc.rhs(u).squeeze()
        if not self.known_solutions:
            return res

        eta = self.__deflation(u.u)
        return eta * res

    def inverse_basis(self):
        return self.colloc.matrix_inverse_basis()

    def adjoint(self, *args, **kwargs):
        return self.colloc.matrix_adjoint(*args, **kwargs)

    def matrix_full(self, *args, **kwargs):
        return self.colloc.matrix_full(*args, **kwargs)

    def precond(self):
        return DeflatedResidualPrecond(self.P, self.b, dtype=self.dtype,
                                       ks=len(self.known_solutions),
                                       functional=self.functional,
                                       eta=self.eta)

    def __deflation(self, u):
        eta = 1.
        if not self.known_solutions:
            return eta

        for ks in self.known_solutions:
            eta *= (self.shift + 1. / h1norm(u - ks)**self.power)

        # Put it all together
        return eta

    def __compute_der_deflation(self, u):
        """ Computes the derivative of the deflation operator eta(u)

            eta'(u) = 2 eta(u) Sum [ u - u_k / [ shift ( | u - u_k |^2 + 1 ) | u - u_k |^2 ] ]
                                k

            This needs to be implemented as a LinearOperator
        """
        eta = 1.
        if not self.known_solutions:
            return eta, None

        # Compute the function that defines the functional
        function = np.zeros_like(u)
        for ks in self.known_solutions:
            diff = u - ks
            diff_norm = h1norm(diff)

            # deflation factor
            defl = self.shift + 1. / diff_norm**self.power
            eta *= defl

            # scale the function appropriately
            function += diff / (defl * diff_norm**(2 + self.power))

        # multiply by the common factor!
        function *= -self.power * eta

        # Make sure that function has the correct shape
        if abs(function.shape[0] - u.shape[0]) > 0:
            function.prolong(u.shape[0])

        # create Functional representing the derivative of eta
        functional = Functional(function, order=1, basis=self.colloc.name)
        return eta, functional

    def _matvec(self, c):
        """ Implements y = Ax

            Here this is eta * (A * x - outer(b, deta))
        """
        # If no known solutions we bail and simply compute the regular part
        if not self.known_solutions:
            # Apply preconditioner
            #return self.M._mul_vector(self.prcond._matvec(c))
            return self.M._mul_vector(c)

        # Compute the derivative of eta
        deta = self.functional(c)

        # The b here should be just F(u)
        return self.eta * self.M._mul_vector(c) + self.b * deta

    def _rmatvec(self, x):
        """ Implements x = A^T y """
        if self.MT is None:
            self.MT = self.M.transpose()

        if not self.known_solutions:
            #return self.prcond._rmatvec(self.MT._mul_vector(x))
            return self.MT._mul_vector(x)

        # Compute the derivative of eta
        deta = self.functional(x)
        assert False, 'FIXME!'

        # The b here should be just F(u)
        return self.eta * self.MT._mul_vector(x) + self.b * deta

    def to_matrix(self):
        """ Faster than tosparse -> since whenever we are not deflating we
            have know the sparse matrix!
        """
        if not self.known_solutions:
            return self.M
        else:
            return self.tosparse()


class DeflatedResidualPrecond(LinearOperator):
    def __init__(self, Pf, b, dtype=None, *args, **kwargs):
        self.Pf = Pf
        self.PfT = None
        self.b = b

        self.shape = self.Pf.shape
        self.dtype = np.dtype(dtype)
        self.explicit = False

        self.ks = kwargs.get('ks', 0)
        self.eta = kwargs.get('eta', 1.0)

        # Pre-compute this!
        self.functional = kwargs.get('functional', None)

        if self.functional is not None:
            self.denom = self.__compute_denom()

    def __compute_denom(self):
        # compute the values for the inner-product
        return self.eta**2 + self.functional(self.b)

    def _matvec(self, x):
        """
        Computes the action of the inverse of

            P_G = eta P_F + F outer d

            which is computed via the Shermann-Morrisson formula
        """
        if self.ks == 0:
            return self.Pf.dot(x)

        term1 = self.Pf.dot(x)
        term2 = self.b * (self.functional(term1) / self.denom)
        return (term1 - term2) / self.eta

    def _rmatvec(self, x):
        if self.PfT is None:
            self.PfT = self.Pf.transpose()

        if self.ks == 0:
            return self.PfT.dot(x)

        assert False, 'FIXME'

    def to_matrix(self):
        # TODO: can we do this faster?
        return self.tosparse()
