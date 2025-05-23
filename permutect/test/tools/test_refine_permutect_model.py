import tempfile
from argparse import Namespace

from tensorboard.backend.event_processing.event_accumulator import EventAccumulator
from permutect import constants
from permutect.architecture.artifact_model import load_model
from permutect.tools import refine_artifact_model


def test_refine_artifact_model():
    # Inputs
    training_data_tarfile = '/Users/davidben/mutect3/permutect/integration-tests/singular-10-Mb/preprocessed-dataset.tar'
    # pretrained_model = '/Users/davidben/mutect3/permutect/integration-tests/singular-10-Mb/permutect-model.pt'
    pretrained_model = '/Users/davidben/mutect3/permutect/integration-tests/dream1-chr20/model.pt'

    # Outputs
    saved_model = tempfile.NamedTemporaryFile()
    training_tensorboard_dir = tempfile.TemporaryDirectory()

    # STEP 2: train a model
    train_model_args = Namespace()
    setattr(train_model_args, constants.CALIBRATION_SOURCES_NAME, None)
    setattr(train_model_args, constants.LEARN_ARTIFACT_SPECTRA_NAME, True)  # could go either way
    setattr(train_model_args, constants.GENOMIC_SPAN_NAME, 100000)

    # Training data inputs
    setattr(train_model_args, constants.TRAIN_TAR_NAME, training_data_tarfile)
    setattr(train_model_args, constants.PRETRAINED_ARTIFACT_MODEL_NAME, pretrained_model)

    # training hyperparameters
    setattr(train_model_args, constants.BATCH_SIZE_NAME, 64)
    setattr(train_model_args, constants.INFERENCE_BATCH_SIZE_NAME, 64)
    setattr(train_model_args, constants.NUM_WORKERS_NAME, 0)
    setattr(train_model_args, constants.NUM_EPOCHS_NAME, 2)
    setattr(train_model_args, constants.NUM_CALIBRATION_EPOCHS_NAME, 1)
    setattr(train_model_args, constants.LEARNING_RATE_NAME, 0.001)
    setattr(train_model_args, constants.WEIGHT_DECAY_NAME, 0.01)

    # path to saved model
    setattr(train_model_args, constants.OUTPUT_NAME, saved_model.name)
    setattr(train_model_args, constants.TENSORBOARD_DIR_NAME, training_tensorboard_dir.name)

    refine_artifact_model.main_without_parsing(train_model_args)

    events = EventAccumulator(training_tensorboard_dir.name)
    events.Reload()

    loaded_model, artifact_log_priors, artifact_spectra_state_dict = load_model(saved_model)

    assert artifact_log_priors is not None
    assert artifact_spectra_state_dict is not None

    print(artifact_log_priors)
    h = 99

