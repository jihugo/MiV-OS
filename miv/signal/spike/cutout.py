__all__ = ["SpikeCutout", "ChannelSpikeCutout"]

from typing import Any, Dict, List, Optional, Tuple, Union

from dataclasses import dataclass

import numpy as np


@dataclass
class SpikeCutout:
    """SpikeCutout class

    Attributes
    ----------
    cutout : Union[np.ndarray, Tuple[np.ndarray, np.ndarray]]
    sampling_rate : float
    pca_comp_index : int
    """

    def __init__(
        self,
        cutout: Union[np.ndarray, Tuple[np.ndarray, np.ndarray]],
        sampling_rate: float,
        pca_comp_index: int,
    ) -> None:
        self.cutout: np.ndarray = np.array(cutout)
        self.sampling_rate: float = sampling_rate
        self.pca_comp_index: int = pca_comp_index

    def __len__(self) -> int:
        return len(self.cutout)


class ChannelSpikeCutout:
    """This class holds the SpikeCutout objects for a single channel

    Attributes
    ----------
    cutouts : np.ndarray
        1D NumPy array of SpikeCutout objects that belong to the same channel
    num_components : int
        Number of components for PCA decomposition
    channel_index : int
    categorized : bool
    categorization_list : Optional[np.ndarray], defualt = None
        List of categorization
        (categorization_list[component index][category index])
    """

    CATEGORY_NAMES = ["uncategorized", "neuronal", "false", "mixed"]

    def __init__(
        self,
        cutouts: np.ndarray,
        num_components: int,
        channel_index: int,
        categorization_list: Optional[np.ndarray] = None,
    ):
        self.cutouts: np.ndarray = cutouts
        self.num_components: int = num_components
        self.channel_index: int = channel_index
        self.categorized: bool = bool(categorization_list)
        self.categorization_list: np.ndarray = (
            np.array(categorization_list)
            if self.categorized
            else np.zeros(num_components)
        )

    def __len__(self) -> int:
        return len(self.cutouts)

    def get_cutouts_by_component(self) -> List[List[SpikeCutout]]:
        """
        Returns
        -------
        2D list of SpikeCutout elements where rows correspond to PCA component indices
        """
        components: List[List[SpikeCutout]] = []
        for row_index in range(self.num_components):
            components.append([])
        for index, cutout in enumerate(self.cutouts):
            components[cutout.pca_comp_index].append(cutout)
        return components

    def categorize(self, category_index: np.ndarray) -> None:
        """
        Categorize the components in this channel with category indices in
        a 1D list where each element corresponds to the component index.

        CATEGORY_NAMES = ["uncategorized", "neuronal", "false", "mixed"]

        Example:
        categorize(np.array([1, 3, 2])) categorizes component 0 as neuronal spikes,
        component 1 as mixed spikes, and 2 as false spikes.
        """
        if len(category_index) < self.num_components:
            self.categorization_list = np.zeros(self.num_components)
            self.categorized = False
        else:
            self.categorization_list = category_index
            self.categorized = True

    def get_labeled_cutouts(self) -> Dict[str, Any]:
        """
        This function returns only the cutouts that were categorized.

        Returns
        -------
        labels :
            1D list of category label index for each spike
        labeled_cutouts :
            1D list of corresponding cutouts
        size :
            int value for the number of labeled cutouts
        """
        labels = []
        labeled_cutouts = []
        size = 0
        if self.categorized:
            for cutout_index, cutout in enumerate(self.cutouts):
                labels.append(self.categorization_list[cutout.pca_comp_index])
                labeled_cutouts.append(cutout)
                size += 1
        return {"labels": labels, "labeled_cutouts": labeled_cutouts, "size": size}
