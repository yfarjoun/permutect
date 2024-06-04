import tempfile
from argparse import Namespace

from tensorboard.backend.event_processing.event_accumulator import EventAccumulator

from permutect import constants
from permutect.tools import train_representation_model
from permutect.architecture.representation_model import load_representation_model


def test_train_representation_model():
    training_data_tarfile = "/Users/davidben/mutect3/permutect/integration-tests/singular-10-Mb/preprocessed-dataset.tar"
    saved_embedding_model = tempfile.NamedTemporaryFile()
    training_tensorboard_dir = tempfile.TemporaryDirectory()

    train_model_args = Namespace()
    setattr(train_model_args, constants.READ_EMBEDDING_DIMENSION_NAME, 18)
    setattr(train_model_args, constants.NUM_TRANSFORMER_HEADS_NAME, 3)
    setattr(train_model_args, constants.TRANSFORMER_HIDDEN_DIMENSION_NAME, 20)
    setattr(train_model_args, constants.NUM_TRANSFORMER_LAYERS_NAME, 2)
    setattr(train_model_args, constants.INFO_LAYERS_NAME, [20, 20])
    setattr(train_model_args, constants.AGGREGATION_LAYERS_NAME, [20, 20, 20])
    cnn_layer_strings = ['convolution/kernel_size=3/out_channels=64',
                     'pool/kernel_size=2',
                     'leaky_relu',
                     'flatten',
                     'linear/out_features=10']
    setattr(train_model_args, constants.REF_SEQ_LAYER_STRINGS_NAME, cnn_layer_strings)
    setattr(train_model_args, constants.DROPOUT_P_NAME, 0.0)
    setattr(train_model_args, constants.ALT_DOWNSAMPLE_NAME, 20)
    setattr(train_model_args, constants.BATCH_NORMALIZE_NAME, False)

    # Training data inputs
    setattr(train_model_args, constants.TRAIN_TAR_NAME, training_data_tarfile)
    setattr(train_model_args, constants.PRETRAINED_MODEL_NAME, None)

    # training hyperparameters
    setattr(train_model_args, constants.REWEIGHTING_RANGE_NAME, 0.3)
    setattr(train_model_args, constants.BATCH_SIZE_NAME, 64)
    setattr(train_model_args, constants.NUM_WORKERS_NAME, 2)
    setattr(train_model_args, constants.NUM_EPOCHS_NAME, 2)
    setattr(train_model_args, constants.NUM_CALIBRATION_EPOCHS_NAME, 0)
    setattr(train_model_args, constants.LEARNING_RATE_NAME, 0.001)
    setattr(train_model_args, constants.WEIGHT_DECAY_NAME, 0.01)

    # path to saved model
    setattr(train_model_args, constants.OUTPUT_NAME, saved_embedding_model.name)
    setattr(train_model_args, constants.TENSORBOARD_DIR_NAME, training_tensorboard_dir.name)

    train_representation_model.main_without_parsing(train_model_args)

    events = EventAccumulator(training_tensorboard_dir.name)
    events.Reload()

    loaded_representation_model = load_representation_model(saved_embedding_model)
