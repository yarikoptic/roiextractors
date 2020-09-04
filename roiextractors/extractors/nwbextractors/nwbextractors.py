import os
import uuid
import numpy as np
import yaml
from datetime import datetime
from collections import abc
from lazy_ops import DatasetView
from pathlib import Path
from warnings import warn
from ...imagingextractor import ImagingExtractor
from ...segmentationextractor import SegmentationExtractor
from ...extraction_tools import PathType, check_get_frames_args, check_get_videos_args, _pixel_mask_extractor
from copy import deepcopy

try:
    from pynwb import NWBHDF5IO, TimeSeries, NWBFile
    from pynwb.base import Images
    from pynwb.image import GrayscaleImage
    from pynwb.ophys import ImageSegmentation, Fluorescence, OpticalChannel, TwoPhotonSeries, DfOverF
    from pynwb.file import Subject
    from pynwb.device import Device
    from hdmf.data_utils import DataChunkIterator

    HAVE_NWB = True
except ModuleNotFoundError:
    HAVE_NWB = False


def nwb_metadata_recursive_update(metadata_base, metadata_input):
    return_dict = deepcopy(metadata_base)
    for base_key, base_val in return_dict.items():
        if metadata_input.get(base_key):
            if isinstance(base_val,dict):
                return_dict[base_key] = nwb_metadata_recursive_update(base_val, metadata_input[base_key])
            elif isinstance(base_val,list):
                if isinstance(metadata_input[base_key], list):
                    list_len = min(len(base_val),len(metadata_input[base_key]))
                    for base_val_num in range(list_len):
                        return_dict[base_key][base_val_num] = \
                            nwb_metadata_recursive_update(return_dict[base_key][base_val_num], metadata_input[base_key][base_val_num])
                else:
                    continue
            else:
                return_dict[base_key] = metadata_input[base_key]
    return return_dict


def check_nwb_install():
    assert HAVE_NWB, "To use the Nwb extractors, install pynwb: \n\n pip install pynwb\n\n"


def set_dynamic_table_property(dynamic_table, ids, row_ids, property_name, values, index=False,
                               default_value=np.nan, description='no description'):
    check_nwb_install()
    if not isinstance(row_ids, list) or not all(isinstance(x, int) for x in row_ids):
        raise TypeError("'ids' must be a list of integers")
    if any([i not in ids for i in row_ids]):
        raise ValueError("'ids' contains values outside the range of existing ids")
    if not isinstance(property_name, str):
        raise TypeError("'property_name' must be a string")
    if len(row_ids) != len(values) and index is False:
        raise ValueError("'ids' and 'values' should be lists of same size")

    if index is False:
        if property_name in dynamic_table:
            for (row_id, value) in zip(row_ids, values):
                dynamic_table[property_name].data[ids.index(row_id)] = value
        else:
            col_data = [default_value] * len(ids)  # init with default val
            for (row_id, value) in zip(row_ids, values):
                col_data[ids.index(row_id)] = value
            dynamic_table.add_column(
                name=property_name,
                description=description,
                data=col_data,
                index=index
            )
    else:
        if property_name in dynamic_table:
            raise NotImplementedError
        else:
            dynamic_table.add_column(
                name=property_name,
                description=description,
                data=values,
                index=index
            )


def get_dynamic_table_property(dynamic_table, *, row_ids=None, property_name):
    all_row_ids = list(dynamic_table.id[:])
    if row_ids is None:
        row_ids = all_row_ids
    return [dynamic_table[property_name][all_row_ids.index(x)] for x in row_ids]


def update_dict(d, u):
    for k, v in u.items():
        if isinstance(v, abc.Mapping):
            d[k] = update_dict(d.get(k, {}), v)
        else:
            d[k] = v
    return d


class NwbImagingExtractor(ImagingExtractor):
    """
    Class used to extract data from the NWB data format. Also implements a
    static method to write any format specific object to NWB.
    """

    extractor_name = 'NwbImaging'
    installed = HAVE_NWB # check at class level if installed or not
    is_writable = True
    mode = 'file'
    installation_mesg = "To use the Nwb Extractor run:\n\n pip install pynwb\n\n"  # error message when not installed

    def __init__(self, file_path, optical_series_name='TwoPhotonSeries'):
        """
        Parameters
        ----------
        file_path: str
            The location of the folder containing dataset.nwb file
        optical_series_name: str (optional)
            optical series to extract data from
        """
        assert HAVE_NWB, self.installation_mesg
        ImagingExtractor.__init__(self)
        self._path = file_path

        self.io = NWBHDF5IO(self._path, 'r')
        self.nwbfile = self.io.read()
        if optical_series_name is not None:
            self._optical_series_name = optical_series_name
        else:
            a_names = list(self.nwbfile.acquisition)
        if len(a_names) > 1:
            raise ValueError('More than one acquisition found. You must specify two_photon_series.')
        if len(a_names) == 0:
            raise ValueError('No acquisitions found in the .nwb file.')
        self._optical_series_name = a_names[0]

        opts = self.nwbfile.acquisition[self._optical_series_name]
        assert isinstance(opts, TwoPhotonSeries), "The optical series must be of type pynwb.TwoPhotonSeries"

        #TODO if external file --> return another proper extractor (e.g. TiffImagingExtractor)
        assert opts.external_file is None, "Only 'raw' format is currently supported"

        if hasattr(opts, 'timestamps') and opts.timestamps:
            self._sampling_frequency = 1. / np.median(np.diff(opts.timestamps))
            self._imaging_start_time = opts.timestamps[0]
        else:
            self._sampling_frequency = opts.rate
        self._imaging_start_time = opts.get(os, 'starting_time', 0.)


        if len(opts.data.shape) == 3:
            self._num_frames, self._size_x, self._size_y = opts.data.shape
            self._num_channels = 1
            self._channel_names = opts.imaging_plane.optical_channel[0].name
        else:
            raise NotImplementedError("4D volumetric data are currently not supported")

            # Fill epochs dictionary
            self._epochs = {}
        if self.nwbfile.epochs is not None:
            df_epochs = self.nwbfile.epochs.to_dataframe()
            # TODO implement add_epoch() method in base class
            self._epochs = {row['tags'][0]: {
                'start_frame': self.time_to_frame(row['start_time']),
                'end_frame': self.time_to_frame(row['stop_time'])}
                for _, row in df_epochs.iterrows()}

            self._kwargs = {'file_path': str(Path(file_path).absolute()),
                            'optical_series_name': optical_series_name}
        self.make_nwb_metadata(nwbfile=self.nwbfile, opts=opts)

    def __del__(self):
        self.io.close()

    def time_to_frame(self, time: FloatType):
        return int((time - self._imaging_start_time) * self.get_sampling_frequency())

    def frame_to_time(self, frame: IntType):
        return float(frame / self.get_sampling_frequency() + self._imaging_start_time)

    def make_nwb_metadata(self, nwbfile, opts):
        # Metadata dictionary - useful for constructing a nwb file
        self.nwb_metadata = dict()
        self.nwb_metadata['NWBFile'] = {
            'session_description': nwbfile.session_description,
            'identifier': nwbfile.identifier,
            'session_start_time': nwbfile.session_start_time,
            'institution': nwbfile.institution,
            'lab': nwbfile.lab
        }
        self.nwb_metadata['Ophys'] = dict()
        # Update metadata with Device info
        self.nwb_metadata['Ophys']['Device'] = []
        for dev in nwbfile.devices:
            self.nwb_metadata['Ophys']['Device'].append({'name': dev})

        # Update metadata with ElectricalSeries info
        self.nwb_metadata['Ophys']['TwoPhotonSeries'] = []
        self.nwb_metadata['Ophys']['TwoPhotonSeries'].append({
            'name': opts.name
        })

    #TODO use lazy_ops
    @check_get_frames_args
    def get_frames(self, frame_idxs, channel=0):
        opts = self.nwbfile.acquisition[self._optical_series_name]
        if frame_idxs.size > 1 and np.all(np.diff(frame_idxs) > 0):
            return opts.data[frame_idxs]
        else:
            sorted_idxs = np.sort(frame_idxs)
            argsorted_idxs = np.argsort(frame_idxs)
            return opts.data[sorted_idxs][argsorted_idxs]

    @check_get_videos_args
    def get_video(self, start_frame=None, end_frame=None, channel=0):
        opts = self.nwbfile.acquisition[self._optical_series_name]
        video = opts.data[start_frame:end_frame]
        return video

    def get_image_size(self):
        return [self._size_x, self._size_y]

    def get_num_frames(self):
        return self._num_frames

    def get_sampling_frequency(self):
        return self._sampling_frequency

    def get_channel_names(self):
        """List of  channels in the recoding.

        Returns
        -------
        channel_names: list
            List of strings of channel names
        """
        return self._channel_names

    def get_num_channels(self):
        """Total number of active channels in the recording

        Returns
        -------
        no_of_channels: int
            integer count of number of channels
        """
        return self._num_channels

    @staticmethod
    def add_devices(imaging, nwbfile, metadata):
        # Devices
        if 'Ophys' not in metadata:
            metadata['Ophys'] = dict()
        if 'Device' not in metadata['Ophys']:
            metadata['Ophys']['Device'] = [{'name': 'Device'}]
        # Tests if devices exist in nwbfile, if not create them from metadata
        for dev in metadata['Ophys']['Device']:
            if dev['name'] not in nwbfile.devices:
                nwbfile.create_device(name=dev['name'])

        return nwbfile

    @staticmethod
    def add_two_photon_series(imaging, nwbfile, metadata):
        """
        Auxiliary static method for nwbextractor.
        Adds two photon series from imaging object as TwoPhotonSeries to nwbfile object.
        """
        if 'Ophys' not in metadata:
            metadata['Ophys'] = {}

        if 'Ophys' not in metadata or 'TwoPthotonSeries' not in metadata['Ophys']:
            metadata['Ophys']['TwoPhotonSeries'] = [{'name': 'TwoPhotonSeries',
                                                     'description': 'optical_series_description'}]
        # Tests if ElectricalSeries already exists in acquisition
        nwb_es_names = [ac for ac in nwbfile.acquisition]
        opts = metadata['Ophys']['TwoPhotonSeries'][0]
        if opts['name'] not in nwb_es_names:
            # retrieve device
            device = nwbfile.devices[list(nwbfile.devices.keys())[0]]

            # create optical channel
            if 'OpticalChannel' not in metadata['Ophys']:
                metadata['Ophys']['OpticalChannel'] = [{'name': 'OpticalChannel',
                                                        'description': 'no description',
                                                        'emission_lambda': 500.}]

            optical_channel = OpticalChannel(**metadata['Ophys']['OpticalChannel'][0])
            # sampling rate
            rate = float(imaging.get_sampling_frequency())

            if 'ImagingPlane' not in metadata['Ophys']:
                metadata['Ophys']['ImagingPlane'] = [{'name': 'ImagingPlane',
                                                      'description': 'no description',
                                                      'excitation_lambda': 600.,
                                                      'indicator': 'Indicator',
                                                      'location': 'Location',
                                                      'grid_spacing': [.01, .01],
                                                      'grid_spacing_unit': 'meters'}]
            imaging_meta = {'optical_channel': optical_channel,
                            'imaging_rate': rate,
                            'device': device}
            metadata['Ophys']['ImagingPlane'][0] = update_dict(metadata['Ophys']['ImagingPlane'][0], imaging_meta)

            imaging_plane = nwbfile.create_imaging_plane(**metadata['Ophys']['ImagingPlane'][0])

            # def data_generator(imaging, channels_ids):
            #     #  generates data chunks for iterator
            #     for id in channels_ids:
            #         data = recording.get_traces(channel_ids=[id]).flatten()
            #         yield data
            #
            # data = data_generator(imaging=imaging, channels_ids=curr_ids)
            # ophys_data = DataChunkIterator(data=data, iter_axis=1)
            acquisition_name = opts['name']

            # using internal data. this data will be stored inside the NWB file
            ophys_ts = TwoPhotonSeries(
                name=acquisition_name,
                data=imaging.get_video(),
                imaging_plane=imaging_plane,
                rate=rate,
                unit='normalized amplitude',
                comments='Generated from RoiInterface::NwbImagingExtractor',
                description='acquisition_description'
            )

            nwbfile.add_acquisition(ophys_ts)

        return nwbfile

    @staticmethod
    def add_epochs(imaging, nwbfile):
        """
        Auxiliary static method for nwbextractor.
        Adds epochs from recording object to nwbfile object.
        """
        # add/update epochs
        for (name, ep) in imaging._epochs.items():
            if nwbfile.epochs is None:
                nwbfile.add_epoch(
                    start_time=imaging.frame_to_time(ep['start_frame']),
                    stop_time=imaging.frame_to_time(ep['end_frame']),
                    tags=name
                )
            else:
                if [name] in nwbfile.epochs['tags'][:]:
                    ind = nwbfile.epochs['tags'][:].index([name])
                    nwbfile.epochs['start_time'].data[ind] = imaging.frame_to_time(ep['start_frame'])
                    nwbfile.epochs['stop_time'].data[ind] = imaging.frame_to_time(ep['end_frame'])
                else:
                    nwbfile.add_epoch(
                        start_time=imaging.frame_to_time(ep['start_frame']),
                        stop_time=imaging.frame_to_time(ep['end_frame']),
                        tags=name
                    )

        return nwbfile

    @staticmethod
    def write_imaging(imaging: ImagingExtractor, save_path: PathType = None, nwbfile=None,
                      metadata: dict = None):
        '''
        Parameters
        ----------
        imaging: ImagingExtractor
        save_path: PathType
            Required if an nwbfile is not passed. Must be the path to the nwbfile
            being appended, otherwise one is created and written.
        nwbfile: NWBFile
            Required if a save_path is not specified. If passed, this function
            will fill the relevant fields within the nwbfile. E.g., calling

            roiextractors.NwbImagingExtractor.write_imaging(
                my_imaging_extractor, my_nwbfile
            )

            will result in the appropriate changes to the my_nwbfile object.
        metadata: dict
            metadata info for constructing the nwb file (optional).
        '''
        assert HAVE_NWB, NwbImagingExtractor.installation_mesg

        if nwbfile is not None:
            assert isinstance(nwbfile, NWBFile), "'nwbfile' should be of type pynwb.NWBFile"

        assert save_path is None or nwbfile is None, \
            'Either pass a save_path location, or nwbfile object, but not both!'

        # Update any previous metadata with user passed dictionary
        if metadata is None:
            metadata = dict()
        if hasattr(imaging, 'nwb_metadata'):
            metadata = update_dict(imaging.nwb_metadata, metadata)

        if nwbfile is None:
            if os.path.exists(save_path):
                read_mode = 'r+'
            else:
                read_mode = 'w'

            with NWBHDF5IO(save_path, mode=read_mode) as io:
                if read_mode == 'r+':
                    nwbfile = io.read()
                else:
                    # Default arguments will be over-written if contained in metadata
                    nwbfile_kwargs = dict(session_description='no description',
                                          identifier=str(uuid.uuid4()),
                                          session_start_time=datetime.now())
                    if 'NWBFile' in metadata:
                        nwbfile_kwargs.update(metadata['NWBFile'])
                    nwbfile = NWBFile(**nwbfile_kwargs)

                    NwbImagingExtractor.add_devices(imaging=imaging,
                                                    nwbfile=nwbfile,
                                                    metadata=metadata)

                    NwbImagingExtractor.add_two_photon_series(imaging=imaging,
                                                              nwbfile=nwbfile,
                                                              metadata=metadata)

                    NwbImagingExtractor.add_epochs(imaging=imaging,
                                                   nwbfile=nwbfile)

                # Write to file
                io.write(nwbfile)
        else:
            NwbImagingExtractor.add_devices(imaging=imaging,
                                            nwbfile=nwbfile,
                                            metadata=metadata)

            NwbImagingExtractor.add_two_photon_series(imaging=imaging,
                                                      nwbfile=nwbfile,
                                                      metadata=metadata)

            NwbImagingExtractor.add_epochs(imaging=imaging,
                                           nwbfile=nwbfile)


class NwbSegmentationExtractor(SegmentationExtractor):

    extractor_name = 'NwbSegmentationExtractor'
    installed = True  # check at class level if installed or not
    is_writable = False
    mode = 'file'
    installation_mesg = ""  # error message when not installed

    def __init__(self, file_path):
        """
        Creating NwbSegmentationExtractor object from nwb file
        Parameters
        ----------
        file_path: str
            .nwb file location
        """
        check_nwb_install()
        SegmentationExtractor.__init__(self)
        if not os.path.exists(file_path):
            raise Exception('file does not exist')

        self.file_path = file_path
        self.image_masks = None
        self._roi_locs = None
        self._accepted_list = None
        self._io = NWBHDF5IO(file_path, mode='r+')
        nwbfile = self._io.read()
        self.nwbfile = nwbfile
        _nwbchildren_type = [type(i).__name__ for i in nwbfile.all_children()]
        _nwbchildren_name = [i.name for i in nwbfile.all_children()]
        _procssing_module = [_nwbchildren_name[f]
                             for f, u in enumerate(_nwbchildren_type) if u == 'ProcessingModule']
        mod = nwbfile.processing[_procssing_module[0]]
        if len(_procssing_module) > 1:
            print('multiple processing modules found, picking the first one')
        elif not mod:
            raise Exception('no processing module found')

        # Extract image_mask/background:
        _plane_segmentation_exist = [i for i, e in enumerate(
            _nwbchildren_type) if e == 'PlaneSegmentation']
        if not _plane_segmentation_exist:
            print('could not find a plane segmentation to contain image mask')
        else:
            ps = nwbfile.all_children()[_plane_segmentation_exist[0]]
        # self.image_masks = np.moveaxis(np.array(ps['image_mask'].data), [0, 1, 2], [2, 0, 1])
        if 'image_mask' in ps.colnames:
            self.image_masks = DatasetView(ps['image_mask'].data).lazy_transpose([1, 2, 0])
        if 'RoiCentroid' in ps.colnames:
            self._roi_locs = ps['RoiCentroid']
        if 'Accepted' in ps.colnames:
            self._accepted_list = ps['Accepted'].data[:]
        # Extract Image dimensions:

        # Extract roi_response:
        roi_names = [_nwbchildren_name[val]
                      for val, i in enumerate(_nwbchildren_type) if i == 'RoiResponseSeries']
        if not roi_names:
            raise Exception('no ROI response series found')
        else:
            for trace_name in ['roiresponseseries', 'dff', 'neuropil', 'deconvolved']:
                trace_name_nwb = [j for j,i in enumerate(roi_names) if trace_name in i.lower()]
                if trace_name_nwb:
                    trace_name_segext = 'raw' if trace_name == 'roiresponseseries' else trace_name
                    setattr(self,f'_roi_response_{trace_name_segext}',
                            mod['Fluorescence'].get_roi_response_series(roi_names[trace_name_nwb[0]]).data[:].T)

        # Extract samp_freq:
        self._sampling_frequency = mod['Fluorescence'].get_roi_response_series(roi_names[0]).rate
        # Extract get_num_rois()/ids:
        self._roi_idx = np.array(ps.id.data)

        # Imaging plane:
        _optical_channel_exist = [i for i, e in enumerate(
            _nwbchildren_type) if e == 'OpticalChannel']
        if _optical_channel_exist:
            self._channel_names = []
            for i in _optical_channel_exist:
                self._channel_names.append(nwbfile.all_children()[i].name)
        # Movie location:
        _image_series_exist = [i for i, e in enumerate(
            _nwbchildren_type) if e == 'TwoPhotonSeries']
        if not _image_series_exist:
            self._extimage_dims = None
        else:
            self._raw_movie_file_location = \
                nwbfile.all_children()[_image_series_exist[0]].external_file[:][0]
            self._extimage_dims = \
                nwbfile.all_children()[_image_series_exist[0]].dimension

        # property name/data extraction:
        self._property_name_exist = [
            i for i in ps.colnames if i not in ['image_mask', 'pixel_mask']]
        self.property_vals = []
        for i in self._property_name_exist:
            self.property_vals.append(np.array(ps[i].data))

        #Extracting stores images as GrayscaleImages:
        _greyscaleimages = [i for i in nwbfile.all_children() if type(i).__name__ == 'GrayscaleImage']
        self._image_correlation = [i.data[()] for i in _greyscaleimages if 'corr' in i.name.lower()][0]
        self._image_mean = [i.data[()] for i in _greyscaleimages if 'mean' in i.name.lower()][0]

    def __del__(self):
        self._io.close()

    def get_accepted_list(self):
        if self._accepted_list is None:
            return list(range(self.get_num_rois()))
        else:
            return np.where(self._accepted_list==1)[0].tolist()

    def get_rejected_list(self):
        return [a for a in self.get_roi_ids() if a not in set(self.get_accepted_list())]

    @property
    def roi_locations(self):
        if self._roi_locs is None:
            return None
        else:
            return self._roi_locs.data[:].T

    def get_roi_ids(self):
        return self._roi_idx

    def get_image_size(self):
        return self._extimage_dims

    @staticmethod
    def get_nwb_metadata(sgmextractor):
        """
        Converts metadata from the segmentation into nwb specific metadata
        Parameters
        ----------
        sgmextractor: SegmentationExtractor
        """
        metadata = {'NWBFile': {'session_start_time': datetime.now(),
                                'identifier': str(uuid.uuid4()),
                                'session_description': 'ses desc'},
                    'ophys': {'Device': [{'name': 'Microscope'}],
                              'Fluorescence': {'roi_response_series':[{'name': 'RoiResponseSeries',
                                                                       'description': 'array of raw fluorescence traces'}]},
                              'ImageSegmentation': {'plane_segmentations': [{'description': 'Segmented ROIs',
                                                                            'name': 'PlaneSegmentation'}]},
                              'ImagingPlane':[{'name': 'ImagingPlane',
                                               'description': 'imaging plane description',
                                               'excitation_lambda': 600.0,
                                               'indicator': 'GCaMP',
                                               'optical_channels': [{'name': 'OpticalChannel',
                                                                     'emission_lambda': 500.0,
                                                                     'description': 'description of optical channel'}]}],
                              'TwoPhotonSeries': [{'name': 'TwoPhotonSeries'}]}}
        # Optical Channel name:
        for i in range(sgmextractor.get_num_channels()):
            ch_name = sgmextractor.get_channel_names()[i]
            if i == 0:
                metadata['ophys']['ImagingPlane'][0]['optical_channels'][i]['name'] = ch_name
            else:
                metadata['ophys']['ImagingPlane'][0]['optical_channels'].append(dict(
                    name=ch_name,
                    emission_lambda=500.0,
                    description=f'{ch_name} description'
                ))

        # set roi_response_series rate:
        rate = np.float('NaN') if sgmextractor.get_sampling_frequency() is None else sgmextractor.get_sampling_frequency()
        for trace_name, trace_data in sgmextractor.get_traces_dict().items():
            if trace_name == 'raw':
                if trace_data is not None:
                    metadata['ophys']['Fluorescence']['roi_response_series'][0].update(rate=rate)
                continue
            if len(trace_data.shape) != 0:
                metadata['ophys']['Fluorescence']['roi_response_series'].append(dict(
                    name=trace_name.capitalize(),
                    description=f'description of {trace_name} traces',
                    rate=rate
                ))
        # TwoPhotonSeries update:
        metadata['ophys']['TwoPhotonSeries'][0].update(
            dimension=sgmextractor.get_image_size())
        return metadata

    @staticmethod
    def write_segmentation(segext_obj, save_path, plane_num=0, metadata=None, file_overwrite=False):
        if os.path.exists(save_path) and not file_overwrite:
            nwbfile_exist = True
            file_mode = 'r+'
        else:
            if os.path.exists(save_path):
                os.remove(save_path)
            if not os.path.exists(os.path.dirname(save_path)):
                os.mkdir(os.path.dirname(save_path))
            nwbfile_exist = False
            file_mode = 'w'
        # parse metadata correctly:
        if segext_obj.extractor_name == 'MultiSegmentationExtractor':
            segext_objs = segext_obj.segmentations
            if metadata is not None and not isinstance(metadata, list):
                raise ValueError('for MultiSegmentationExtractor enter metadata as a list of SegmentationExtractor metadata')
        else:
            segext_objs = [segext_obj]
        metadata_base_list = [NwbSegmentationExtractor.get_nwb_metadata(sgobj) for sgobj in segext_objs]
        print(f'writing nwb for {segext_obj.extractor_name}\n')
        # updating base metadata with new:
        for num, data in enumerate(metadata_base_list):
            metadata_input = metadata[num] if metadata else {}
            metadata_base_list[num] = nwb_metadata_recursive_update(metadata_base_list[num], metadata_input)
        #loop for every plane:
        with NWBHDF5IO(save_path, file_mode) as io:
            metadata_base_common = metadata_base_list[0]
            if nwbfile_exist:
                nwbfile = io.read()
            else:
                nwbfile = NWBFile(**metadata_base_common['NWBFile'])
                # Subject:
                if metadata_base_common.get('Subject'):
                    nwbfile.subject = Subject(**metadata_base_common['Subject'])

            # Processing Module:
            if 'ophys' not in nwbfile.processing:
                ophys = nwbfile.create_processing_module('ophys',
                                                             'contains optical physiology processed data')
            else:
                ophys = nwbfile.get_processing_module('ophys')

            for plane_no_loop, (segext_obj, metadata) in enumerate(zip(segext_objs, metadata_base_list)):
                # Device:
                if metadata['ophys']['Device'][0]['name'] not in nwbfile.devices:
                    nwbfile.create_device(**metadata['ophys']['Device'][0])

                # ImageSegmentation:
                image_segmentation_name = 'ImageSegmentation' if plane_no_loop==0 else f'ImageSegmentation_Plane{plane_no_loop}'
                if image_segmentation_name not in ophys.data_interfaces:
                    image_segmentation = ImageSegmentation(name=image_segmentation_name)
                    ophys.add_data_interface(image_segmentation)

                # OpticalChannel:
                optical_channels = [OpticalChannel(**i) for i in metadata['ophys']['ImagingPlane'][0]['optical_channels']]

                # ImagingPlane:
                image_plane_name = 'ImagingPlane' if plane_no_loop == 0 else f'ImagePlane_{plane_no_loop}'
                if image_plane_name not in nwbfile.imaging_planes.keys():
                    input_kwargs = dict(
                        name=image_plane_name,
                        description='no description',
                        device=nwbfile.get_device(metadata_base_common['ophys']['Device'][0]['name']),
                        excitation_lambda=np.nan,
                        imaging_rate=1.0,
                        indicator='unknown',
                        location='unknown'
                    )
                    _ = metadata['ophys']['ImagingPlane'][0].pop('optical_channels')
                    metadata['ophys']['ImagingPlane'][0].update(optical_channel=optical_channels)
                    input_kwargs.update(**metadata['ophys']['ImagingPlane'][0])
                    imaging_plane = nwbfile.create_imaging_plane(**input_kwargs)
                else:
                    imaging_plane = nwbfile.imaging_planes[image_plane_name]

                # PlaneSegmentation:
                input_kwargs = dict(
                    description='output from segmenting my favorite imaging plane',
                    imaging_plane=imaging_plane
                )
                if metadata['ophys']['ImageSegmentation']['plane_segmentations'][0]['name'] not in image_segmentation.plane_segmentations:
                    input_kwargs.update(**metadata['ophys']['ImageSegmentation']['plane_segmentations'][0])
                    ps = image_segmentation.create_plane_segmentation(**input_kwargs)
                    ps_exist = False
                else:
                    ps = image_segmentation.get_plane_segmentation(i['name'])
                    ps_exist = True

                # ROI add:
                image_masks = segext_obj.get_roi_image_masks()
                roi_ids = segext_obj.get_roi_ids()
                accepted_ids = [1 if k in segext_obj.get_accepted_list() else 0 for k in roi_ids]
                roi_locations = np.array(segext_obj.get_roi_locations()).T
                if not ps_exist:
                    ps.add_column(name='RoiCentroid',
                                  description='x,y location of centroid of the roi in image_mask')
                    ps.add_column(name='Accepted',
                                  description='1 if ROi was accepted or 0 if rejected as a cell during segmentation operation')
                for num, row in enumerate(roi_ids): #Expects the existing ps to be a prior nwbsegext saved nwb file with existing columns
                    ps.add_row(id=row, image_mask=image_masks[:, :, num],
                               RoiCentroid=roi_locations[num,:],
                               Accepted=accepted_ids[num])

                # Fluorescence Traces:
                if 'Flourescence' not in ophys.data_interfaces:
                    fluorescence = Fluorescence()
                    ophys.add_data_interface(fluorescence)
                else:
                    fluorescence = ophys.data_interfaces['Fluorescence']
                roi_response_dict = segext_obj.get_traces_dict()
                roi_table_region = ps.create_roi_table_region(description=f'region for Imaging plane{plane_no_loop}',
                                                              region=list(range(segext_obj.get_num_rois())))
                rate = np.float('NaN') if segext_obj.get_sampling_frequency() is None else segext_obj.get_sampling_frequency()
                for i, j in roi_response_dict.items():
                    data = getattr(segext_obj, f'_roi_response_{i}')
                    if data is not None:
                        trace_name = 'RoiResponseSeries' if i == 'raw' else i.capitalize()
                        trace_name = trace_name if plane_no_loop==0 else trace_name+f'_Plane{plane_no_loop}'
                        input_kwargs = dict(name=trace_name, data=data.T, rois=roi_table_region, rate=rate)
                        if trace_name not in fluorescence.roi_response_series:
                            fluorescence.create_roi_response_series(**input_kwargs)

                # create Two Photon Series:
                if 'TwoPhotonSeries' not in nwbfile.acquisition:
                    warn('could not find TwoPhotonSeries, using ImagingExtractor to create an nwbfile')

                # adding images:
                images_dict = segext_obj.get_images_dict()
                images_name = 'SegmentationImages' if plane_no_loop==0 else f'SegmentationImages_Plane{plane_no_loop}'
                if images_name not in ophys.data_interfaces:
                    images = Images(images_name)
                    for img_name, img_no in images_dict.items():
                        if img_no is not None:
                            images.add_image(GrayscaleImage(name=img_name, data=img_no))
                    ophys.add(images)

            # saving NWB file:
            io.write(nwbfile)

        # test read
        with NWBHDF5IO(save_path, 'r') as io:
            io.read()
