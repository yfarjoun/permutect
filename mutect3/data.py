import random
from typing import List

import torch
from torch.utils.data import Dataset, DataLoader
from torch.utils.data.sampler import Sampler

from mutect3 import tensors, utils

# TODO: this should change eventually by having normal artifact table w/ ALT and REF, just like
# the other table.  For now, we just hack a SNV, DELETION, INDEL

EPSILON = 0.00001
DATA_COUNT_FOR_QUANTILES = 10000


def normal_artifact_type(item: tensors.Datum):
    ref = item.site_info().ref()
    alt = item.site_info().alt()
    diff = len(alt) - len(ref)
    return "SNV" if diff == 0 else ("INSERTION" if diff > 0 else "DELETION")


# Read sets have different sizes so we can't form a batch by naively stacking tensors.  We need a custom way
# to collate a list of Datum into a Batch

# collated batch contains:
# 2D tensors of ALL ref (alt) reads, not separated by set.
# number of reads in ref (alt) read sets, in same order as read tensors
# info: 2D tensor of info fields, one row per variant
# labels: 1D tensor of 0 if non-artifact, 1 if artifact
# lists of original mutect2_data and site info
# Example: if we have two input data, one with alt reads [[0,1,2], [3,4,5] and the other with
# alt reads [[6,7,8], [9,10,11], [12,13,14] then the output alt reads tensor is
# [[0,1,2], [3,4,5], [6,7,8], [9,10,11], [12,13,14]] and the output counts are [2,3]
# inside the model, the counts will be used to separate the reads into sets
class Batch:

    # given list of slice sizes, produce a list of index slice objects
    # eg input = [2,3,1] --> [slice(0,2), slice(2,5), slice(5,6)]
    def make_slices(sizes, offset=0):
        slice_ends = offset + torch.cumsum(sizes, dim=0)
        return [slice(offset if n == 0 else slice_ends[n - 1], slice_ends[n]) for n in range(len(sizes))]

    def __init__(self, data: List[tensors.Datum]):
        self._original_list = data  # keep this for downsampling augmentation
        self.labeled = data[0].artifact_label() is not None
        for datum in data:
            if (datum.artifact_label() is not None) != self.labeled:
                raise Exception("Batch may not mix labeled and unlabeled")

        self._ref_counts = torch.IntTensor([len(item.ref_tensor()) for item in data])
        self._alt_counts = torch.IntTensor([len(item.alt_tensor()) for item in data])
        self._ref_slices = Batch.make_slices(self._ref_counts)
        self._alt_slices = Batch.make_slices(self._alt_counts, torch.sum(self._ref_counts))
        self._reads = torch.cat([item.ref_tensor() for item in data] + [item.alt_tensor() for item in data], dim=0)
        self._info = torch.stack([item.info_tensor() for item in data], dim=0)
        self._labels = torch.FloatTensor([item.artifact_label() for item in data]) if self.labeled else None
        self._site_info = [item.site_info() for item in data]
        self._mutect2_data = [item.mutect_info() for item in data]
        self._size = len(data)

        # TODO: variant type needs to go in constructor -- and maybe it should be utils.VariantType, not str
        normal_artifact_data = [tensors.NormalArtifactDatum(item.normal_alt_count(), item.normal_depth(),
                                                            len(item.alt_tensor()),
                                                            len(item.alt_tensor()) + len(item.ref_tensor()),
                                                            1.0, normal_artifact_type(item)) for item in data]
        self._normal_artifact_batch = NormalArtifactBatch(normal_artifact_data)

    def augmented_copy(self, beta):
        return Batch([datum.downsampled_copy(beta) for datum in self._original_list])

    def is_labeled(self):
        return self.labeled

    def size(self):
        return self._size

    def reads(self):
        return self._reads

    def ref_slices(self):
        return self._ref_slices

    def alt_slices(self):
        return self._alt_slices

    def ref_counts(self):
        return self._ref_counts

    def alt_counts(self):
        return self._alt_counts

    def info(self):
        return self._info

    def site_info(self):
        return self._site_info

    def mutect_info(self):
        return self._mutect2_data

    def labels(self):
        return self._labels

    def normal_artifact_batch(self):
        return self._normal_artifact_batch


# TODO bring this into the class
def medians_and_iqrs(tensor_2d):
    # column medians etc
    medians = torch.quantile(tensor_2d, 0.5, dim=0, keepdim=False)
    vals = [0.05, 0.01, 0.0]
    iqrs = [torch.quantile(tensor_2d, 1 - x, dim=0, keepdim=False) - torch.quantile(tensor_2d, x, dim=0, keepdim=False)
            for x in vals]

    # for each element, try first the IQR, but if it's zero try successively larger ranges
    adjusted_iqrs = []
    for n in range(len(medians)):
        # if all zero, add 1 for no scaling
        value_to_append = 1.0
        for iqr in iqrs:
            # add the first non-zero scale
            if iqr[n] > EPSILON:
                value_to_append = iqr[n]
                break
        adjusted_iqrs.append(value_to_append)
    return medians, torch.FloatTensor(adjusted_iqrs)


class Mutect3Dataset(Dataset):
    def __init__(self, data: List[tensors.Datum], shuffle=False):
        self.data = data
        if shuffle:
            random.shuffle(self.data)

        # concatenate a bunch of ref tensors and take element-by-element quantiles
        ref = torch.cat([datum.ref_tensor() for datum in self.data[:DATA_COUNT_FOR_QUANTILES]], dim=0)
        info = torch.stack([datum.info_tensor() for datum in self.data[:DATA_COUNT_FOR_QUANTILES]], dim=0)

        self.read_medians, self.read_iqrs = medians_and_iqrs(ref)
        self.info_medians, self.info_iqrs = medians_and_iqrs(info)

    def __len__(self):
        return len(self.data)

    def __getitem__(self, index):
        raw = self.data[index]
        ref = (raw.ref_tensor() - self.read_medians) / self.read_iqrs
        alt = (raw.alt_tensor() - self.read_medians) / self.read_iqrs
        info = (raw.info_tensor() - self.info_medians) / self.info_iqrs
        return tensors.Datum(ref, alt, info, raw.site_info(), raw.mutect_info(), raw.artifact_label(),
                             raw.normal_depth(), raw.normal_alt_count())


def mutect3_dataset_from_pickles(pickles, shuffle=False):
    data = []
    for pickled_file in pickles:
        data.extend(tensors.load_pickle(pickled_file))
    return Mutect3Dataset(data, shuffle)


def make_training_and_validation_datasets(training_pickles):
    # make our training, validation, and testing data
    train_and_valid = mutect3_dataset_from_pickles(training_pickles, shuffle=True)
    train, valid = utils.split_dataset_into_train_and_valid(train_and_valid, 0.9)

    unlabeled_count = sum([1 for datum in train_and_valid if datum.artifact_label() is None])
    print("Unlabeled data: " + str(unlabeled_count) + ", labeled data: " + str(len(train_and_valid) - unlabeled_count))
    print("Dataset sizes -- training: " + str(len(train)) + ", validation: " + str(len(valid)))
    return train, valid


def chunk(indices, chunk_size):
    return torch.split(torch.tensor(indices), chunk_size)


# make batches that are all supervised or all unsupervised
# the model handles balancing the losses between supervised and unsupervised in training, so we don't need to worry
# it's convenient to have equal numbers of labeled and unlabeled batches, so we adjust the unlabeled batch size
class SemiSupervisedBatchSampler(Sampler):
    def __init__(self, dataset: Mutect3Dataset, batch_size):
        self.artifact_indices = [n for n in range(len(dataset)) if dataset[n].artifact_label() == 1]
        self.non_artifact_indices = [n for n in range(len(dataset)) if dataset[n].artifact_label() == 0]
        self.unlabeled_indices = [n for n in range(len(dataset)) if dataset[n].artifact_label() is None]
        self.batch_size = batch_size

    # randomly sample non-artifact indices to get a balanced training set
    def __iter__(self):
        random.shuffle(self.artifact_indices)
        random.shuffle(self.non_artifact_indices)
        random.shuffle(self.unlabeled_indices)
        artifact_count = min(len(self.artifact_indices), len(self.non_artifact_indices))

        # balanced dataset in each epoch -- labeled vs unlabeled and artifact vs non-artifact
        labeled_indices = self.artifact_indices[:artifact_count] + self.non_artifact_indices[:artifact_count]
        random.shuffle(labeled_indices)

        unlabeled_batch_size = round((len(labeled_indices) / len(self.unlabeled_indices)) * self.batch_size)

        labeled_batches = chunk(labeled_indices, unlabeled_batch_size)
        unlabeled_batches = chunk(self.unlabeled_indices, self.batch_size)
        combined = [batch.tolist() for batch in list(labeled_batches + unlabeled_batches)]
        random.shuffle(combined)
        return iter(combined)

    def __len__(self):
        return len(self.artifact_indices) * 2 // self.batch_size + len(self.artifact_indices) // self.batch_size


# this is used for training and validation but not deployment / testing
def make_semisupervised_data_loader(training_dataset, batch_size):
    sampler = SemiSupervisedBatchSampler(training_dataset, batch_size)
    return DataLoader(dataset=training_dataset, batch_sampler=sampler, collate_fn=Batch)


def make_test_data_loader(test_dataset, batch_size):
    return DataLoader(dataset=test_dataset, batch_size=batch_size, collate_fn=Batch)


class NormalArtifactDataset(Dataset):
    def __init__(self, pickled_files):
        self.data = []
        for pickled_file in pickled_files:
            self.data.extend(tensors.load_pickle(pickled_file))
        random.shuffle(self.data)

    def __len__(self):
        return len(self.data)

    def __getitem__(self, index):
        return self.data[index]


class NormalArtifactBatch:

    def __init__(self, data: List[tensors.NormalArtifactDatum]):
        self._normal_alt = torch.IntTensor([datum.normal_alt_count() for datum in data])
        self._normal_depth = torch.IntTensor([datum.normal_depth() for datum in data])
        self._tumor_alt = torch.IntTensor([datum.tumor_alt_count() for datum in data])
        self._tumor_depth = torch.IntTensor([datum.tumor_depth() for datum in data])
        self._downsampling = torch.FloatTensor([datum.downsampling() for datum in data])
        self._variant_type = [datum.variant_type() for datum in data]
        self._size = len(data)

    def size(self):
        return self._size

    def normal_alt(self):
        return self._normal_alt

    def normal_depth(self):
        return self._normal_depth

    def tumor_alt(self):
        return self._tumor_alt

    def tumor_depth(self):
        return self._tumor_depth

    def downsampling(self):
        return self._downsampling

    def variant_type(self):
        return self._variant_type


def make_normal_artifact_data_loader(dataset: NormalArtifactDataset, batch_size):
    return DataLoader(dataset=dataset, batch_size=batch_size, collate_fn=NormalArtifactBatch)
