import math
from typing import List

import torch
from permutect import utils
from torch import nn, exp, unsqueeze, logsumexp
from torch.nn.functional import softmax, log_softmax

from permutect.architecture.mlp import MLP
from permutect.metrics.plotting import simple_plot
from permutect.utils import beta_binomial, gamma_binomial, binomial, Variation


class OverdispersedBinomialMixture(nn.Module):
    """
    This model takes in 2D tensor inputs (1st dimension is batch, 2nd is a feature vector that in practice is one-hot
    encoding of variant type) and as a function of input has a Beta OR Gamma mixture model.  That is, it computes for each input
    vector 1) a vector of mixture component weights 2) a vector of the alpha shape parameters of each component and 3) a
    vector of beta shape parameters of each component.  Due to batching these are all represented as 2D tensors.

    Note that both the Beta and Gamma distributions' shape parameters are traditionally called "alpha" and "beta".

    The computed likelihoods take in a 1D batch of total counts n and 1D batch of "success" counts k.

    It optionally has a max mean that scales every mean to some amount less than or equal to 1, which is useful when we want
    to force the mixture to represent only small fractions.

    When using a BetaBinomial mixture, due to conjugacy the integral over the latent probability of success (in our uses,
    this is the allele fraction of variants or artifacts) is exact and we use a closed form analytic expression for the
    density of a BetaBinomial.  That is, the probability (k = alt count, n = depth, f = latent allele fraction)

    P(k|n, alpha, beta) = integral{Beta(f|alpha, beta) * Binomial(k|n, f)}

    is exact.

    When using a GammaBinomial mixture, i.e. one with a Gamma prior Gamma(f, alpha, beta) the cannot do the integral exactly.
    However, the *binomial* factor Binom(k|n,f), which as a function of f is a Beta distribution, is extremely well-approximated
    by a Gamma distribution, and the product of this Gamma approximation and the Gamma prior on f *is* exactly integrable.

    The approximation breaks down if the allele fractions are not small (since then the support of the Gamma for f > 1
    becaomes significant), so we should only use the Gamma prior version to model artifact allele fractions.

    In addition to 'beta' and 'gamma' modes, there is also the 'none' mode which has no overdispersion in the individual components.
    That is, each component is a plain binomial, though of course by virtue of being a mixture the distribution as a whole is overdispersed.
    """

    def __init__(self, input_size: int, num_components: int, max_mean: float = 1, mode: str = 'beta', hidden_layers: List[int] = []):
        super(OverdispersedBinomialMixture, self).__init__()
        self.mode = mode
        self.num_components = num_components
        self.input_size = input_size
        self.max_mean = max_mean

        self.weights_pre_softmax = MLP(layer_sizes=[input_size] + hidden_layers + [num_components])
        self.mean_pre_sigmoid = MLP(layer_sizes=[input_size] + hidden_layers + [num_components])
        self.max_concentration = torch.nn.Parameter(torch.tensor(50.0))
        self.concentration_pre_sigmoid = MLP(layer_sizes=[input_size] + hidden_layers + [num_components])

        # TODO: replace this now that we use MLP instead of linear?
        # the kth column of weights corresponds to the kth index of input = 1 and other inputs = 0
        # we are going to manually initialize to equal weights -- all zeroes
        # the alphas and betas will be equally spaced Beta distributions, for example, each column of alpha would be
        # 1, 11, 21, 31, 41, 51 and each column of beta would be 51, 41, 31, 21, 11, 1
        #with torch.no_grad():
        #    self.weights_pre_softmax.weight.copy_(torch.zeros_like(self.weights_pre_softmax.weight))
        #    self.concentration_pre_exp.weight.copy_(
        #        torch.log(10 * num_components * torch.ones_like(self.concentration_pre_exp.weight)))
        #    self.set_means((0.5 + torch.arange(num_components)) / num_components)

    # TODO: replace this now that we use MLP instead of linear?
    #def set_means(self, means):
    #    assert len(means) == self.num_components
    #    each_mean_col_pre_sigmoid = torch.log(means / (1 - means))
    #    repeated_mean_cols = torch.hstack(self.input_size * [each_mean_col_pre_sigmoid.unsqueeze(dim=1)])
    #    with torch.no_grad():
    #        self.mean_pre_sigmoid.weight.copy_(repeated_mean_cols)

    def set_weights(self, weights):
        assert len(weights) == self.num_components
        pre_softmax_weights = torch.log(weights)
        repeated = torch.hstack(self.input_size * [pre_softmax_weights.unsqueeze(dim=1)])
        with torch.no_grad():
            self.weights_pre_softmax.weight.copy_(repeated)

    '''
    here x is a 2D tensor, 1st dimension batch, 2nd dimension being features that determine which Beta mixture to use
    n and k are 1D tensors, the only dimension being batch.
    '''
    def forward(self, x, n, k):
        assert x.dim() == 2
        assert n.size() == k.size()
        assert len(x) == len(n)
        assert x.size()[1] == self.input_size

        log_weights = log_softmax(self.weights_pre_softmax(x), dim=1)

        # we make them 2D, with 1st dim batch, to match alpha and beta.  A single column is OK because the single value of
        # n/k are broadcast over all mixture components
        n_2d = unsqueeze(n, dim=1)
        k_2d = unsqueeze(k, dim=1)

        # 2D tensors -- 1st dim batch, 2nd dim mixture component
        means = self.max_mean * torch.sigmoid(self.mean_pre_sigmoid(x))
        concentrations = self.get_concentration(x)

        if self.mode == 'beta':
            alphas = means * concentrations
            betas = (1 - means) * concentrations
            log_component_likelihoods = beta_binomial(n_2d, k_2d, alphas, betas)
        elif self.mode == 'gamma':
            alphas = means * concentrations
            betas = concentrations
            log_component_likelihoods = gamma_binomial(n_2d, k_2d, alphas, betas)
        elif self.mode == 'none':
            # each mean is the center of a binomial
            log_component_likelihoods = binomial(n_2d, k_2d, means)
        else:
            raise Exception("we don't have that kind of mode!")

        log_weighted_component_likelihoods = log_weights + log_component_likelihoods

        # yields one number per batch, squeezed into 1D output tensor
        return logsumexp(log_weighted_component_likelihoods, dim=1, keepdim=False)

    def get_concentration(self, input_2d):
        return self.max_concentration * torch.sigmoid(self.concentration_pre_sigmoid(input_2d))

    # given 1D input tensor, return 1D tensors of component alphas and betas
    def component_shapes(self, input_1d):
        assert input_1d.dim() == 1

        input_2d = input_1d.unsqueeze(dim=0)
        means = self.max_mean * torch.sigmoid(self.mean_pre_sigmoid(input_2d)).squeeze()
        concentrations = self.get_concentration(input_2d).squeeze()
        alphas = means * concentrations
        betas = (1 - means) * concentrations if self.mode == 'beta' else concentrations
        return alphas, betas

    def component_weights(self, input_1d):
        assert input_1d.dim() == 1
        input_2d = input_1d.unsqueeze(dim=0)
        return softmax(self.weights_pre_softmax(input_2d), dim=1).squeeze()

    # given 1D input tensor, return the moments E[x], E[ln(x)], and E[x ln(x)] of the underlying beta mixture
    def moments_of_underlying_beta_mixture(self, input_1d):
        assert input_1d.dim() == 1
        assert self.mode == 'beta'
        alphas, betas = self.component_shapes(input_1d)
        weights = self.component_weights(input_1d)

        # E[x]
        component_means = alphas / (alphas + betas)
        mixture_mean = torch.sum(weights * component_means)

        # E[ln(x)]
        component_log_means = torch.digamma(alphas) - torch.digamma(
            alphas + betas)  # digamma broadcasts to make 1D tensor
        mixture_log_mean = torch.sum(weights * component_log_means)

        # E[x ln(x)]
        component_log_linear_means = component_means * (torch.digamma(alphas + 1) - torch.digamma(alphas + betas + 1))
        mixture_log_linear_mean = torch.sum(weights * component_log_linear_means)

        return mixture_mean, mixture_log_mean, mixture_log_linear_mean

    '''
    here x is a 2D tensor, 1st dimension batch, 2nd dimension being features that determine which Beta mixture to use
    n is a 1D tensor, the only dimension being batch, and we sample a 1D tensor of k's
    '''
    def sample(self, x, n):
        assert x.dim() == 2
        assert x.size()[1] == self.input_size
        assert n.dim() == 1
        assert len(x) == len(n)

        # compute weights and select one mixture component from the corresponding multinomial for each datum / row
        weights = softmax(self.weights_pre_softmax(x).detach(), dim=1)  # 2D tensor
        component_indices = torch.multinomial(weights, num_samples=1, replacement=True)  # 2D tensor with one column

        # get 1D tensors of one selected alpha and beta shape parameter per datum / row, then sample a fraction from each
        # It may be very wasteful computing everything and only using one component, but this is just for unit testing
        means = self.max_mean * torch.sigmoid(self.mean_pre_sigmoid(x).detach()).gather(dim=1, index=component_indices).squeeze()
        concentrations = self.get_concentration(x).detach().gather(dim=1, index=component_indices).squeeze()
        alphas = means * concentrations
        betas = (1 - means) * concentrations if self.mode == 'beta' else concentrations
        dist = torch.distributions.beta.Beta(alphas, betas) if self.mode == 'beta' else torch.distributions.gamma.Gamma(alphas, betas)
        fractions = dist.sample()  # 1D tensor

        # recall, n and fractions are 1D tensors; result is also 1D tensor, one "success" count per datum
        return torch.distributions.binomial.Binomial(total_count=n, probs=fractions).sample()

    def fit(self, num_epochs, inputs_2d_tensor, depths_1d_tensor, alt_counts_1d_tensor, batch_size=64):
        assert inputs_2d_tensor.dim() == 2
        assert depths_1d_tensor.dim() == 1
        assert alt_counts_1d_tensor.dim() == 1
        assert len(depths_1d_tensor) == len(alt_counts_1d_tensor)

        optimizer = torch.optim.Adam(self.parameters())
        num_batches = math.ceil(len(alt_counts_1d_tensor) / batch_size)

        for epoch in range(num_epochs):
            for batch in range(num_batches):
                batch_start = batch * batch_size
                batch_end = min(batch_start + batch_size, len(alt_counts_1d_tensor))
                batch_slice = slice(batch_start, batch_end)
                loss = -torch.mean(self.forward(inputs_2d_tensor[batch_slice], depths_1d_tensor[batch_slice],
                                                alt_counts_1d_tensor[batch_slice]))
                utils.backpropagate(optimizer, loss)

    '''
    get raw data for a spectrum plot of probability density vs allele fraction.  
    here x is a 1D tensor, a single datum/row of the 2D tensors as above
    '''
    def spectrum_density_vs_fraction(self, variant_type: Variation, depth: int):
        device = next(self.mean_pre_sigmoid.parameters()).device
        fractions = torch.arange(0.01, 0.99, 0.001)  # 1D tensor on CPU
        x = torch.from_numpy(variant_type.one_hot_tensor()).float().to(device)

        unsqueezed = x.unsqueeze(dim=0)  # this and the three following tensors are 2D tensors with one row
        log_weights = log_softmax(self.weights_pre_softmax(unsqueezed).detach(), dim=1).cpu()
        means = self.max_mean * torch.sigmoid(self.mean_pre_sigmoid(unsqueezed).detach()).cpu()

        # now we're on CPU
        if self.mode == 'none':
            # this is copied from the beta case below -- basically we smear each delta function / discrete binomial
            # into a narrow Gaussian
            dist = torch.distributions.normal.Normal(means, 0.01 * torch.ones_like(means))
            densities = exp(torch.logsumexp(log_weights + dist.log_prob(fractions.unsqueeze(dim=1)), dim=1,
                                            keepdim=False))  # 1D tensor
            return fractions, densities
        else:
            concentrations = self.get_concentration(unsqueezed).detach().cpu()
            alphas = means * concentrations
            betas = (1 - means) * concentrations if self.mode == 'beta' else concentrations

            # since f.unsqueeze(dim=1) is 2D column vector, log_prob produces 2D tensor where row index is f and column index is mixture component
            # adding the single-row 2D tensor log_weights broadcasts to each row / value of f
            # then we apply log_sum_exp, dim= 1, to sum over components and get a log_density for each f
            dist = torch.distributions.beta.Beta(alphas, betas) if self.mode == 'beta' else torch.distributions.gamma.Gamma(alphas, betas)
            densities = exp(torch.logsumexp(log_weights + dist.log_prob(fractions.unsqueeze(dim=1)), dim=1, keepdim=False))  # 1D tensor

            return fractions, densities

    '''
    here x is a 1D tensor, a single datum/row of the 2D tensors as above
    '''
    def plot_spectrum(self, x, title, depth: int):
        fractions, densities = self.spectrum_density_vs_fraction(x, depth)
        return simple_plot([(fractions.numpy(), densities.numpy(), " ")], "AF", "density", title)


