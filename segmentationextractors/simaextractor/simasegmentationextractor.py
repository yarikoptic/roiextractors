import numpy as np
import h5py
from ..segmentationextractor import SegmentationExtractor
from ..writenwb import write_nwb
import sima


class SimaSegmentationExtractor(SegmentationExtractor):
    ''' SegmentationExtractor class:
        input all releveant data and metadata related to the main analysis file parsed by h5py

        Arguments:

        masks:
            description: binary image for each of the regions of interest
            type: np.ndarray (dimensions: # of pixels(d1 X d2 length) x # of ROIs), 2-D

        signal:
            description: fluorescence response of each of the ROI in time
            type: np.ndarray (dimensions: # of ROIs x # timesteps), 2-D

        background_signal:
            description: fluorescence response of each of the background ROIs in time
            type: np.ndarray (dimensions: # of BackgroundRegions x # timesteps), 2-D

        background_masks:
            description: binary image for the background ROIs
            type: np.ndarray (dimensions: # of pixels(d1 X d2) x # of ROIs), 2-D

        summary_image:
            description: mean or the correlation image
            type: np.ndarray (dimensions: d1 x d2)

        roi_idx:
            description: ids of the ROIs
            type: np.ndarray (dimensions: 1 x # of ROIs)

        roi_locs:
            description: x and y location of centroid of ROI mask
            type: np.ndarray (dimensions: # of ROIs x 2)

        samp_freq:
            description: frame rate
            type: np.ndarray (dimensions: 1D)

    '''
    def __init__(self, filepath, sima_segmentation_label='auto_ROIs'):

        self.filepath = filepath
        self.dataset_file = self._file_extractor_read()
        self.sima_segmentation_label = sima_segmentation_label
        self.image_masks, self.extimage_dims, self.raw_images =\
            self._image_mask_extracter_read()
        self.pixel_masks = self._pixel_mask_extracter_read()
        self.roi_response = self._trace_extracter_read()
        self.cn = self._summary_image_read()
        self.total_time = self._tot_exptime_txtractor_read()
        self.filetype = self._file_type_extractor_read()
        self.raw_data_file_location = self._raw_datafile_read()
        # Not found data:
        self._no_background_comps = 1
        self._roi_ids = None
        self._roi_locs = None  # current default implementation
        self._no_rois = None
        self._samp_freq = None
        self._num_of_frames = None
        self.snr_comp = np.nan * np.ones(self.roi_response.shape)
        self.r_values = np.nan * np.ones(self.roi_response.shape)
        self.cnn_preds = np.nan * np.ones(self.roi_response.shape)
        self._rejected_list = []  # remains to mine from mat file or nan it
        self._accepted_list = None  # remains to mine from mat file or nan it
        self.idx_components = self.accepted_list  # remains to mine from mat file or nan it
        self.idx_components_bad = self.rejected_list
        self.image_masks_bk = np.nan * np.ones([self.image_masks.shape[0], self._no_background_comps])
        self.roi_response_bk = np.nan * np.ones([self._no_background_comps, self.roi_response.shape[1]])
        # file close:
        self._file_close()

    def _file_close(self):
        self.dataset_file.close()

    def _file_extractor_read(self):
        _img_dataset = sima.ImagingDataset.load(self.filepath)
        return _img_dataset

    def _image_mask_extracter_read(self):
        _sima_rois = self.dataset_file.ROIs
        if len(_sima_rois) > 1:
            if self.sima_segmentation_label in list(_sima_rois.keys()):
                _sima_rois_data = _sima_rois[self.sima_segmentation_label]
            else:
                raise Exception('Enter a valid name of ROIs from: {}'.format(
                    ','.join(list(_sima_rois.keys()))))
        elif len(_sima_rois) == 1:
            _sima_rois_data = _sima_rois.values()[0]
            self.sima_segmentation_label = _sima_rois.keys()[0]
        else:
            raise Exception('no ROIs found in the sima file')

        image_masks_ = [roi_dat.mask for roi_dat in _sima_rois_data]
        raw_images = np.array(image_masks_).transpose()
        _raw_images_trans = np.array(raw_images).transpose()
        return _raw_images_trans.reshape(
                                [np.prod(_raw_images_trans.shape[0:2]),
                                 _raw_images_trans.shape[2]],
                                 order='F'),\
            _raw_images_trans.shape[0:2],\
            _raw_images_trans

    def _pixel_mask_extracter_read(self):
        _raw_images_trans = self.raw_images
        temp = np.empty((1, 4))
        for i, roiid in enumerate(self.roi_idx):
            _locs = np.where(_raw_images_trans[:, :, i] > 0)
            _pix_values = _raw_images_trans[_raw_images_trans[:, :, i] > 0]
            temp = np.append(temp, np.concatenate(
                                _locs[0].reshape([1, np.size(_locs[0])]),
                                _locs[1].reshape([1, np.size(_locs[1])]),
                                _pix_values.reshape([1, np.size(_locs[1])]),
                                roiid * np.ones(1, np.size(_locs[1]))).T, axis=0)
        return temp[1::,:]

    def _trace_extracter_read(self):
        extracted_signals = self.dataset_file.extract(
            rois=self.sima_segmentation_label, save_summary=False)['raw'][0].T
        return np.array(extracted_signals).T

    def _tot_exptime_txtractor_read(self):
        return None

    def _file_type_extractor_read(self):
        return self.filepath.split('.')[1]

    def _summary_image_read(self):
        summary_images_ = np.squeeze(self.dataset_file.time_averages[0]).T
        return np.array(summary_images_).T

    def _raw_datafile_read(self):
        try:
            return self.dataset_file.sequences[0]._path
        except AttributeError:
            return self.dataset_file.sequences[0]._sequences[0]._path

    @property
    def image_dims(self):
        return list(self.extimage_dims)

    @property
    def no_rois(self):
        if self._no_rois is None:
            raw_images = self.image_masks
            return raw_images.shape[1]
        else:
            return self._no_rois

    @property
    def roi_idx(self):
        if self._roi_ids is None:
            return list(range(self.no_rois))
        else:
            return self._roi_ids

    @property
    def accepted_list(self):
        if self._accepted_list is None:
            return list(range(self.no_rois))
        else:
            return self._accepted_list

    @property
    def rejected_list(self):
        return [a for a in range(self.no_rois) if a not in set(self.accepted_list)]

    @property
    def roi_locs(self):
        if self._roi_locs is None:
            no_ROIs = self.no_rois
            raw_images = self.image_masks
            roi_location = np.ndarray([2, no_ROIs], dtype='int')
            for i in range(no_ROIs):
                temp = np.where(raw_images[:, :, i] == np.amax(raw_images[:, :, i]))
                roi_location[:, i] = np.array([temp[0][0], temp[1][0]]).T
            return roi_location

    @property
    def num_of_frames(self):
        if self._num_of_frames is None:
            extracted_signals = self.roi_response
            return extracted_signals.shape[1]
        else:
            return self._num_of_frames

    @property
    def samp_freq(self):
        time = self.total_time
        nframes = self.num_of_frames
        return nframes / time

    @staticmethod
    def write_recording(segmentation_object, savepath):
        raise NotImplementedError

    # defining the abstract class enformed methods:
    def get_roi_ids(self):
        return self.roi_idx

    def get_num_rois(self):
        return self.no_rois

    def get_roi_locations(self, ROI_ids=None):
        if ROI_ids is None:
            return self.roi_locs
        else:
            return self.roi_locs[:, ROI_ids]

    def get_num_frames(self):
        return self.num_of_frames

    def get_sampling_frequency(self):
        return self.samp_freq

    def get_traces(self, ROI_ids=None, start_frame=None, end_frame=None):
        if start_frame is None:
            start_frame = 0
        if end_frame is None:
            end_frame = self.get_num_frames()
        if ROI_ids is None:
            ROI_ids = range(self.get_roi_ids())
        return self.roi_response[ROI_ids, start_frame:end_frame]

    def get_image_masks(self, ROI_ids=None):
        if ROI_ids is None:
            ROI_ids = range(self.get_roi_ids())
        return self.image_masks.reshape([*self.image_dims, *self.no_rois])[:, :, ROI_ids]

    def get_pixel_masks(self, ROI_ids=None):
        if ROI_ids is None:
            ROI_ids = range(self.get_roi_ids())
        temp = np.empty((1, 4))
        for i, roiid in enumerate(ROI_ids):
            temp = \
                np.append(temp, self.pixel_masks[self.pixel_masks[:, 3] == roiid, :], axis=0)
        return temp[1::, :]

    def get_movie_framesize(self):
        return self.image_dims

    def get_raw_file(self):
        return self.raw_data_file_location
