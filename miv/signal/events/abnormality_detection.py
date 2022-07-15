__all__ = ["AbnormalityDetector"]

from typing import Any, Dict, List, Optional

import numpy as np
import quantities as pq
import tensorflow as tf
from neo import SpikeTrain
from sklearn.utils import shuffle
from tqdm import tqdm

from miv.io import Data, DataManager
from miv.signal.events.protocol import SpikeClassificationModelProtocol
from miv.signal.filter import ButterBandpass, FilterProtocol
from miv.signal.spike import (
    ChannelSpikeCutout,
    SpikeCutout,
    SpikeDetectionProtocol,
    SpikeFeatureExtractionProtocol,
)
from miv.typing import SpikestampsType
from miv.visualization import extract_waveforms


class AbnormalityDetector:
    """Abnormality Detector
    The initialization of this class if the first step in the process.
    With initialization, cutouts for the spontaneous spikes are generated.

        Attributes
        ----------
        spontaneous_data : Data
        experiment_data : Data
        spont_signal_filter : FilterProtocol
            Spontaneous signal filter used prior to spike detection
        spont_spike_detector : SpikeDetectionProtocol
            Spontaneous spike detector used to get spiketrains from signal
        spike_feature_extractor : SpikeFeatureExtractionProtocol
            Spike feature extraction object (PCA, Wavelet, etc.) used to group
            the spontaneous cutouts
        extractor_decomposition_parameter : int, default = 3
    """

    def __init__(
        self,
        spontaneous_data: Data,
        spont_signal_filter: FilterProtocol,
        spont_spike_detector: SpikeDetectionProtocol,
        spike_feature_extractor: SpikeFeatureExtractionProtocol,
        extractor_decomposition_parameter: int = 3,
    ):
        self.spontaneous_data: Data = spontaneous_data
        self.spont_signal_filter: FilterProtocol = spont_signal_filter
        self.spont_spike_detector: SpikeDetectionProtocol = spont_spike_detector
        self.extractor: SpikeFeatureExtractionProtocol = spike_feature_extractor
        self.extractor_decomposition_parameter: int = extractor_decomposition_parameter
        self.trained: bool = False
        self.categorized: bool = False
        self.model: SpikeClassificationModelProtocol

        self.skipped_channels: List[int] = []
        self.test_cutouts = np.array([])
        self.test_labels = np.array([])
        self.model_size: int = 0  # number of labeled spikes used to train model

        # Generate cutouts for spontaneous recording
        self.num_channels: int = 0
        self.spontaneous_cutouts = self._get_cutouts(
            spontaneous_data,
            spont_signal_filter,
            spont_spike_detector,
            spike_feature_extractor,
        )

    def _get_cutouts(
        self,
        data: Data,
        signal_filter: FilterProtocol,
        spike_detector: SpikeDetectionProtocol,
        spike_feature_extractor: SpikeFeatureExtractionProtocol,
    ) -> List[ChannelSpikeCutout]:
        with data.load() as (sig, times, samp):
            spontaneous_sig = signal_filter(sig, samp)
            spontaneous_spikes = spike_detector(spontaneous_sig, times, samp)
            self.num_channels = spontaneous_sig.shape[1]

            self.skipped_channels = []  # Channels with not enough spikes for cutouts
            exp_cutouts = []  # List of SpikeCutout objects
            for chan_index in tqdm(range(self.num_channels)):
                if (
                    spontaneous_spikes[chan_index].shape[0]
                    >= self.extractor_decomposition_parameter
                ):
                    channel_cutouts_list: List[SpikeCutout] = []
                    raw_cutouts = extract_waveforms(
                        spontaneous_sig, spontaneous_spikes, chan_index, samp
                    )
                    labels, transformed = spike_feature_extractor.project(
                        self.extractor_decomposition_parameter, raw_cutouts
                    )

                    for cutout_index, raw_cutout in enumerate(raw_cutouts):
                        channel_cutouts_list.append(
                            SpikeCutout(
                                raw_cutout,
                                samp,
                                labels[cutout_index],
                                spontaneous_spikes[chan_index][cutout_index],
                            )
                        )
                    exp_cutouts.append(
                        ChannelSpikeCutout(
                            np.array(channel_cutouts_list),
                            self.extractor_decomposition_parameter,
                            chan_index,
                        )
                    )
                else:
                    self.skipped_channels.append(chan_index)
                    exp_cutouts.append(
                        ChannelSpikeCutout(
                            np.array([]),
                            self.extractor_decomposition_parameter,
                            chan_index,
                        )
                    )
        return exp_cutouts

    def categorize_spontaneous(
        self, categorization_list: List[List[int]]  # list[chan_index][comp_index]
    ) -> None:
        """Categorize the spontaneous recording components.
        This is the second step in the process of abnormality detection.
        This categorization provides training data for the next step.

        Parameters
        ----------
        categorization_list: List[List[int]]
            The categorization given to components of channels of the spontaneous recording.
            This is a 2D list. The row index represents the channel index. The column
            index represents the extractor component index.
        """
        for chan_index, chan_row in enumerate(categorization_list):
            self.spontaneous_cutouts[chan_index].categorize(np.array(chan_row))
        self.categorized = True

    def train_model(
        self, model: Optional[SpikeClassificationModelProtocol] = None, epochs: int = 5
    ) -> None:
        """Create and train model for cutout recognition
        This is the third step in the process of abnormality detection.

        Parameters
        ----------
        model : SpikeClassificationModelProtocol
            The classification model used.
            By passing None, a default keras model will be built with one hidden layer
            that is the same size as the number of sample points in the cutout.
        epochs : int, default = 5
            The number of iterations for model training
        """
        # Get the labeled cutouts
        labeled_cutouts = []
        labels = []
        self.model_size = 0
        for chan_index, channelCutout in enumerate(self.spontaneous_cutouts):
            channel_labeled_cutouts = channelCutout.get_labeled_cutouts()
            for spike_index, spike_label in enumerate(
                channel_labeled_cutouts["labels"]
            ):
                labels.extend([spike_label])
                labeled_cutouts.append(
                    list(channel_labeled_cutouts["labeled_cutouts"][spike_index])
                )
            self.model_size += channel_labeled_cutouts["size"]

        # Shuffle the cutouts and split into training and test portions
        labeled_cutouts, labels = shuffle(labeled_cutouts, labels)
        split = int(self.model_size * 0.8)
        train_cutouts = np.array(labeled_cutouts[:split])
        train_labels = np.array(labels[:split])
        self.test_cutouts = np.array(labeled_cutouts[split:])
        self.test_labels = np.array(labels[split:])

        # Set up model (if not passed as argument)
        if model is None:
            hidden_layer_size = len(self.spontaneous_cutouts[0].cutouts[0].cutout)
            self._create_default_model(
                np.shape(labeled_cutouts[1]),
                hidden_layer_size,
                len(self.spontaneous_cutouts[0].CATEGORY_NAMES) - 1,
            )
        else:
            self.model = model

        # Train model
        self.model.fit(train_cutouts, train_labels, epochs=epochs)
        self.trained = True

    def evaluate_model(
        self,
        test_cutouts: Optional[np.ndarray] = None,
        test_labels: Optional[np.ndarray] = None,
    ) -> Dict[str, Any]:
        """Evaluate the trained model

        Parameters
        ----------
        test_cutouts : Optional[np.ndarray], default = None
            Raw 2D numpy array of rows of cutout sample points
        test_labels : Optional[np.ndarray], default = None
            1D numpy array of categorization indices

        Returns
        -------
        test_loss : float
            1 if model was not trained
        test_accuracy : float
            0 if model was not trained
        """

        if not self.trained:
            return {"test_loss": 1, "test_accuracy": 0}

        data = test_cutouts if test_cutouts else self.test_cutouts
        labels = test_labels if test_labels else self.test_labels

        loss, acc = self.model.evaluate(data, labels)
        return {"test_loss": loss, "test_accuracy": acc}

    def _create_default_model(self, input_size, hidden_size, output_size) -> None:
        layers = [
            tf.keras.layers.Dense(input_size),
            tf.keras.layers.Dense(hidden_size),
            tf.keras.layers.Dense(output_size),
        ]
        self.model = tf.keras.Sequential(layers)
        self.model.compile(
            optimizer="adam",
            loss=tf.keras.losses.SparseCategoricalCrossentropy(from_logits=True),
            metrics=["accuracy"],
        )

    def get_only_neuronal_spikes(
        self,
        exp_data: Data,
        accuracy_threshold: float = 0.9,
        signal_filter: Optional[FilterProtocol] = None,
        spike_detector: Optional[SpikeDetectionProtocol] = None,
    ) -> List[SpikestampsType]:
        """This method takes each signle individual spike in the experiment data
        and uses the trained model to predict whether the spike is neuronal.

        Parameters
        ----------
        exp_data : Data
            Experiment data
        accuracy_threshold : float, defualt = 0.9
            The category prediction comes in probabilities for each category.
            If the probability for "neuronal" is higher than this threshold,
            the spike will be included.
        signal_filter : Optional[FilterProtocol], default = None
            Filter applied to the experiment data before spike detection.
            If left empty or None, the same filter for the spontaneous data
            will be used.
        spike_detector : Optional[SpikeDetectionProtocol], default = None
            The spike detector used to detect spikes on the experiment data.
            If left empty or None, the same spike detector for the spontaneous
            data will be used.

        Returns
        -------
        (Same return format as SpikeDetectionProtocol)
        A list of SpikestampsType for each channel
        """
        if not self.trained:
            raise Exception("Abnormality detector is not trained yet.")

        exp_filter = signal_filter if signal_filter else self.spont_signal_filter
        exp_detector = spike_detector if spike_detector else self.spont_spike_detector
        list_of_cutout_channels = self._get_cutouts(
            exp_data, exp_filter, exp_detector, self.extractor
        )
        new_spiketrains: List[SpikestampsType] = []

        prob_model = tf.keras.Sequential([self.model, tf.keras.layers.Softmax()])
        for chan_index, cutout_channel in tqdm(enumerate(list_of_cutout_channels)):
            times = []
            t_stop = 0
            for cutout_index, spike_cutout in enumerate(cutout_channel.cutouts):
                prediction = prob_model.predict(
                    np.array([spike_cutout.cutout]), verbose=0
                )
                if prediction[0][0] >= accuracy_threshold:
                    times.append(spike_cutout.time)
                    t_stop = spike_cutout.time
            new_spiketrains.append(SpikeTrain(times, t_stop, pq.s))

        return new_spiketrains

    def get_only_neuronal_components(
        self,
        exp_data: Data,
        accuracy_threshold: float = 0.9,
        signal_filter: Optional[FilterProtocol] = None,
        spike_detector: Optional[SpikeDetectionProtocol] = None,
    ) -> List[SpikestampsType]:
        """This method first takes each spike in a single component group and
        computer an average spike. Then, this average spike is used to determine
        whether the component is neuronal and should be returned.

        Parameters
        ----------
        exp_data : Data
            Experiment data
        accuracy_threshold : float, defualt = 0.9
            The category prediction comes in probabilities for each category.
            If the probability for "neuronal" is higher than this threshold,
            the spikes in the component will be included.
        signal_filter : Optional[FilterProtocol], default = None
            Filter applied to the experiment data before spike detection.
            If left empty or None, the same filter for the spontaneous data
            will be used.
        spike_detector : Optional[SpikeDetectionProtocol], default = None
            The spike detector used to detect spikes on the experiment data.
            If left empty or None, the same spike detector for the spontaneous
            data will be used.

        Returns
        -------
        (Same return format as SpikeDetectionProtocol)
        A list of SpikestampsType for each channel
        """
        if not self.trained:
            raise Exception("Abnormality detector is not trained yet.")

        exp_filter = signal_filter if signal_filter else self.spont_signal_filter
        exp_detector = spike_detector if spike_detector else self.spont_spike_detector
        list_of_cutout_channels = self._get_cutouts(
            exp_data, exp_filter, exp_detector, self.extractor
        )
        new_spiketrains: List[SpikestampsType] = []
        prob_model = tf.keras.Sequential([self.model, tf.keras.layers.Softmax()])

        for chan_index, cutout_channel in tqdm(enumerate(list_of_cutout_channels)):
            chan_cutouts_by_comp = cutout_channel.get_cutouts_by_component()
            channel_times = []

            for comp_index, comp_cutouts in enumerate(chan_cutouts_by_comp):
                loaded_comp_cutouts: List[List[float]] = []

                for cutout_index, spike_cutout in comp_cutouts:
                    loaded_comp_cutouts.append(spike_cutout.cutout)

                comp_mean_cutout = np.mean(loaded_comp_cutouts, axis=0)
                prediction = prob_model.predict(comp_mean_cutout, verbose=0)

                if prediction[0] >= accuracy_threshold:
                    for cutout_index, spike_cutout in comp_cutouts:
                        channel_times.append(spike_cutout.time)

            t_stop = 0 if not (channel_times) else channel_times[-1]
            new_spiketrains.append(SpikeTrain(channel_times, t_stop, pq.s))

        return new_spiketrains
