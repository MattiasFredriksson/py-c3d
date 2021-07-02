'''A Python module for reading and writing C3D files.'''
from __future__ import unicode_literals

import sys
import io
import copy
import numpy as np
import struct
import warnings
from src.manager import Manager
from src.header import Header
from src.group import GroupData, GroupWritable, GroupReadonly
from src.dtypes import DataTypes
from src.utils import is_integer, is_iterable, DEC_to_IEEE_BYTES


class Reader(Manager):
    '''This class provides methods for reading the data in a C3D file.

    A C3D file contains metadata and frame-based data describing 3D motion.

    You can iterate over the frames in the file by calling `read_frames()` after
    construction:

    >>> r = c3d.Reader(open('capture.c3d', 'rb'))
    >>> for frame_no, points, analog in r.read_frames():
    ...     print('{0.shape} points in this frame'.format(points))
    '''

    def __init__(self, handle):
        '''Initialize this C3D file by reading header and parameter data.

        Parameters
        ----------
        handle : file handle
            Read metadata and C3D motion frames from the given file handle. This
            handle is assumed to be `seek`-able and `read`-able. The handle must
            remain open for the life of the `Reader` instance. The `Reader` does
            not `close` the handle.

        Raises
        ------
        ValueError
            If the processor metadata in the C3D file is anything other than 84
            (Intel format).
        '''
        super(Reader, self).__init__(Header(handle))

        self._handle = handle

        def seek_param_section_header():
            ''' Seek to and read the first 4 byte of the parameter header section '''
            self._handle.seek((self._header.parameter_block - 1) * 512)
            # metadata header
            return self._handle.read(4)

        # Begin by reading the processor type:
        buf = seek_param_section_header()
        _, _, parameter_blocks, processor = struct.unpack('BBBB', buf)
        self._dtypes = DataTypes(processor)
        # Convert header parameters in accordance with the processor type (MIPS format re-reads the header)
        self._header._processor_convert(self._dtypes, handle)

        # Restart reading the parameter header after parsing processor type
        buf = seek_param_section_header()

        start_byte = self._handle.tell()
        endbyte = start_byte + 512 * parameter_blocks - 4
        while self._handle.tell() < endbyte:
            chars_in_name, group_id = struct.unpack('bb', self._handle.read(2))
            if group_id == 0 or chars_in_name == 0:
                # we've reached the end of the parameter section.
                break
            name = self._dtypes.decode_string(self._handle.read(abs(chars_in_name))).upper()

            # Read the byte segment associated with the parameter and create a
            # separate binary stream object from the data.
            offset_to_next, = struct.unpack(['<h', '>h'][self._dtypes.is_mips], self._handle.read(2))
            if offset_to_next == 0:
                # Last parameter, as number of bytes are unknown,
                # read the remaining bytes in the parameter section.
                bytes = self._handle.read(endbyte - self._handle.tell())
            else:
                bytes = self._handle.read(offset_to_next - 2)
            buf = io.BytesIO(bytes)

            if group_id > 0:
                # We've just started reading a parameter. If its group doesn't
                # exist, create a blank one. add the parameter to the group.
                group = self._groups.setdefault(group_id, GroupData(self._dtypes))
                group.add_param(name, handle=buf)
            else:
                # We've just started reading a group. If a group with the
                # appropriate numerical id exists already (because we've
                # already created it for a parameter), just set the name of
                # the group. Otherwise, add a new group.
                group_id = abs(group_id)
                size, = struct.unpack('B', buf.read(1))
                desc = size and buf.read(size) or ''
                group = self._get(group_id)
                if group is not None:
                    self._rename_group(group, name)  # Inserts name key
                    group.desc = desc
                else:
                    self._add_group(group_id, name, desc)

        self._check_metadata()

    def read_frames(self, copy=True, analog_transform=True, check_nan=True, camera_sum=False):
        '''Iterate over the data frames from our C3D file handle.

        Parameters
        ----------
        copy : bool
            If False, the reader returns a reference to the same data buffers
            for every frame. The default is True, which causes the reader to
            return a unique data buffer for each frame. Set this to False if you
            consume frames as you iterate over them, or True if you store them
            for later.

        Returns
        -------
        frames : sequence of (frame number, points, analog)
            This method generates a sequence of (frame number, points, analog)
            tuples, one tuple per frame. The first element of each tuple is the
            frame number. The second is a numpy array of parsed, 5D point data
            and the third element of each tuple is a numpy array of analog
            values that were recorded during the frame. (Often the analog data
            are sampled at a higher frequency than the 3D point data, resulting
            in multiple analog frames per frame of point data.)

            The first three columns in the returned point data are the (x, y, z)
            coordinates of the observed motion capture point. The fourth column
            is an estimate of the error for this particular point, and the fifth
            column is the number of cameras that observed the point in question.
            Both the fourth and fifth values are -1 if the point is considered
            to be invalid.
        '''
        # Point magnitude scalar, if scale parameter is < 0 data is floating point
        # (in which case the magnitude is the absolute value)
        scale_mag = abs(self.point_scale)
        is_float = self.point_scale < 0

        if is_float:
            point_word_bytes = 4
        else:
            point_word_bytes = 2
        points = np.zeros((self.point_used, 5), np.float32)

        # TODO: handle ANALOG:BITS parameter here!
        p = self.get('ANALOG:FORMAT')
        analog_unsigned = p and p.string_value.strip().upper() == 'UNSIGNED'
        if is_float:
            analog_dtype = self._dtypes.float32
            analog_word_bytes = 4
        elif analog_unsigned:
            # Note*: Floating point is 'always' defined for both analog and point data, according to the standard.
            analog_dtype = self._dtypes.uint16
            analog_word_bytes = 2
            # Verify BITS parameter for analog
            p = self.get('ANALOG:BITS')
            if p and p._as_integer_value / 8 != analog_word_bytes:
                raise NotImplementedError('Analog data using {} bits is not supported.'.format(p._as_integer_value))
        else:
            analog_dtype = self._dtypes.int16
            analog_word_bytes = 2

        analog = np.array([], float)
        analog_scales, analog_offsets = self.get_analog_transform()

        # Seek to the start point of the data blocks
        self._handle.seek((self._header.data_block - 1) * 512)
        # Number of values (words) read in regard to POINT/ANALOG data
        N_point = 4 * self.point_used
        N_analog = self.analog_used * self.analog_per_frame

        # Total bytes per frame
        point_bytes = N_point * point_word_bytes
        analog_bytes = N_analog * analog_word_bytes
        # Parse the data blocks
        for frame_no in range(self.first_frame, self.last_frame + 1):
            # Read the byte data (used) for the block
            raw_bytes = self._handle.read(N_point * point_word_bytes)
            raw_analog = self._handle.read(N_analog * analog_word_bytes)
            # Verify read pointers (any of the two can be assumed to be 0)
            if len(raw_bytes) < point_bytes:
                warnings.warn('''reached end of file (EOF) while reading POINT data at frame index {}
                                 and file pointer {}!'''.format(frame_no - self.first_frame, self._handle.tell()))
                return
            if len(raw_analog) < analog_bytes:
                warnings.warn('''reached end of file (EOF) while reading POINT data at frame index {}
                                 and file pointer {}!'''.format(frame_no - self.first_frame, self._handle.tell()))
                return

            if is_float:
                # Convert every 4 byte words to a float-32 reprensentation
                # (the fourth column is still not a float32 representation)
                if self._dtypes.is_dec:
                    # Convert each of the first 6 16-bit words from DEC to IEEE float
                    points[:, :4] = DEC_to_IEEE_BYTES(raw_bytes).reshape((self.point_used, 4))
                else:  # If IEEE or MIPS:
                    # Convert each of the first 6 16-bit words to native float
                    points[:, :4] = np.frombuffer(raw_bytes,
                                                  dtype=self._dtypes.float32,
                                                  count=N_point).reshape((self.point_used, 4))

                # Cast last word to signed integer in system endian format
                last_word = points[:, 3].astype(np.int32)

            else:
                # View the bytes as signed 16-bit integers
                raw = np.frombuffer(raw_bytes,
                                    dtype=self._dtypes.int16,
                                    count=N_point).reshape((self.point_used, 4))
                # Read the first six 16-bit words as x, y, z coordinates
                points[:, :3] = raw[:, :3] * scale_mag
                # Cast last word to signed integer in system endian format
                last_word = raw[:, 3].astype(np.int16)

            # Parse camera-observed bits and residuals.
            # Notes:
            # - Invalid sample if residual is equal to -1 (check if word < 0).
            # - A residual of 0.0 represent modeled data (filtered or interpolated).
            # - Camera and residual words are always 8-bit (1 byte), never 16-bit.
            # - If floating point, the byte words are encoded in an integer cast to a float,
            #    and are written directly in byte form (see the MLS guide).
            ##
            # Read the residual and camera byte words (Note* if 32 bit word negative sign is discarded).
            residual_byte, camera_byte = (last_word & 0x00ff), (last_word & 0x7f00) >> 8

            # Fourth value is floating-point (scaled) error estimate (residual)
            points[:, 3] = residual_byte * scale_mag

            # Determine invalid samples
            invalid = last_word < 0
            if check_nan:
                is_nan = ~np.all(np.isfinite(points[:, :4]), axis=1)
                points[is_nan, :3] = 0.0
                invalid &= is_nan
            # Update discarded - sign
            points[invalid, 3] = -1


            # Fifth value is the camera-observation byte
            if camera_sum:
                # Convert to observation sum
                points[:, 4] = sum((camera_byte & (1 << k)) >> k for k in range(8))
            else:
                points[:, 4] = camera_byte #.astype(np.float32)

            # Check if analog data exist, and parse if so
            if N_analog > 0:
                if is_float and self._dtypes.is_dec:
                    # Convert each of the 16-bit words from DEC to IEEE float
                    analog = DEC_to_IEEE_BYTES(raw_analog)
                else:
                    # Integer or INTEL/MIPS floating point data can be parsed directly
                    analog = np.frombuffer(raw_analog, dtype=analog_dtype, count=N_analog)

                # Reformat and convert
                analog = analog.reshape((-1, self.analog_used)).T
                analog = analog.astype(float)
                # Convert analog
                analog = (analog - analog_offsets) * analog_scales

            # Output buffers
            if copy:
                yield frame_no, points.copy(), analog  # .copy(), a new array is generated per frame for analog data.
            else:
                yield frame_no, points, analog

        # Function evaluating EOF, note that data section is written in blocks of 512
        final_byte_index = self._handle.tell()
        self._handle.seek(0, 2)  # os.SEEK_END)
        # Check if more then 1 block remain
        if self._handle.tell() - final_byte_index >= 512:
            warnings.warn('incomplete reading of data blocks. {} bytes remained after all datablocks were read!'.format(
                self._handle.tell() - final_byte_index))

    @property
    def proc_type(self):
        '''Get the processory type associated with the data format in the file.
        '''
        return self._dtypes.proc_type

    def to_writer(self, conversion=None):
        ''' Convert to 'Writer' using the conversion mode.
            See Writer.from_reader() for supported conversion modes and possible exceptions.
        '''
        return Writer.from_reader(self, conversion=conversion)

    def get(self, key, default=None):
        '''Get a readonly group or parameter.

        Parameters
        ----------
        key : str
            If this string contains a period (.), then the part before the
            period will be used to retrieve a group, and the part after the
            period will be used to retrieve a parameter from that group. If this
            string does not contain a period, then just a group will be
            returned.
        default : any
            Return this value if the named group and parameter are not found.

        Returns
        -------
        value : :class:`GroupReadonly` or :class:`Param`
            Either a group or parameter with the specified name(s). If neither
            is found, returns the default value.
        '''
        val = self._get(key)
        if val is None:
            return default
        return val.readonly()

    def items(self):
        ''' Acquire iterable over parameter group pairs.

        Returns
        -------
        items : Touple of ((str, :class:`Group`), ...)
            Python touple containing pairs of name keys and parameter group entries.
        '''
        return ((k, GroupReadonly(v)) for k, v in self._groups.items() if isinstance(k, str))

    def values(self):
        ''' Acquire iterable over parameter group entries.

        Returns
        -------
        values : Touple of (:class:`Group`, ...)
            Python touple containing unique parameter group entries.
        '''
        return (GroupReadonly(v) for k, v in self._groups.items() if isinstance(k, str))

class Writer(Manager):
    '''This class writes metadata and frames to a C3D file.

    For example, to read an existing C3D file, apply some sort of data
    processing to the frames, and write out another C3D file::

    >>> r = c3d.Reader(open('data.c3d', 'rb'))
    >>> w = c3d.Writer()
    >>> w.add_frames(process_frames_somehow(r.read_frames()))
    >>> with open('smoothed.c3d', 'wb') as handle:
    >>>     w.write(handle)

    Parameters
    ----------
    point_rate : float, optional
        The frame rate of the data. Defaults to 480.
    analog_rate : float, optional
        The number of analog samples per frame. Defaults to 0.
    point_scale : float, optional
        The scale factor for point data. Defaults to -1 (i.e., "check the
        POINT:SCALE parameter").
    point_units : str, optional
        The units that the point numbers represent. Defaults to ``'mm  '``.
    gen_scale : float, optional
        General scaling factor for analog data. Defaults to 1.
    '''

    def __init__(self,
                 point_rate=480.,
                 analog_rate=0.,
                 point_scale=-1.):
        '''Set metadata for this writer.

        '''
        self._dtypes = DataTypes() # Only support INTEL format from writing
        super(Writer, self).__init__()

        # Header properties
        self._header.frame_rate = np.float32(point_rate)
        self._header.scale_factor = np.float32(point_scale)
        self.analog_rate = analog_rate
        self._frames = []

    @staticmethod
    def from_reader(reader, conversion=None):
        '''
        source : 'class' Manager
            Source to copy.
        conversion : str
            Conversion mode, None is equivalent to the default mode. Supported modes are:
                'consume'       - (Default) Reader object will be consumed and explicitly deleted.
                'copy'          - Reader objects will be deep copied.
                'copy_metadata' - Similar to 'copy' but only copies metadata and not point and analog frame data.
                'copy_shallow'  - Similar to 'copy' but group parameters are not copied.
                'copy_header'   - Similar to 'copy_shallow' but only the header is copied (frame data is not copied).

        Returns
        -------
        param : :class:`Writer`
            A writeable and persistent representation of the 'Reader' object.

        Raises
        ------
        ValueError
            If mode string is not equivalent to one of the supported modes.
            If attempting to convert non-Intel files using mode other than 'shallow_copy'.
        '''
        writer = Writer()
        # Modes
        is_header_only = conversion == 'copy_header'
        is_meta_copy = conversion == 'copy_metadata'
        is_meta_only = is_header_only or is_meta_copy
        is_consume = conversion == 'consume' or conversion is None
        is_shallow_copy = conversion == 'shallow_copy' or is_header_only
        is_deep_copy = conversion == 'copy' or is_meta_copy
        # Verify mode
        if not (is_consume or is_shallow_copy or is_deep_copy):
            raise ValueError(
                "Unknown mode argument %s. Supported modes are: 'consume', 'copy', or 'shallow_copy'".format(
                conversion))
        if not reader._dtypes.is_ieee and not is_shallow_copy:
            # Can't copy/consume non-Intel files due to the uncertainty of converting parameter data.
            raise ValueError(
                "File was read in %s format and only 'shallow_copy' mode is supported for non Intel files!".format(
                reader._dtypes.proc_type))

        if is_consume:
            writer._header = reader._header
            writer._groups = reader._groups
        elif is_deep_copy:
            writer._header = copy.deepcopy(reader._header)
            writer._groups = copy.deepcopy(reader._groups)
        elif is_shallow_copy:
            # Only copy header (no groups)
            writer._header = copy.deepcopy(reader._header)
            # Reformat header events
            writer._header.encode_events(writer._header.events)

            # Transfer a minimal set parameters
            writer.set_start_frame(reader.first_frame)
            writer.set_point_labels(reader.point_labels)
            writer.set_analog_labels(reader.analog_labels)

            gen_scale, analog_scales, analog_offsets = reader.get_analog_transform_parameters()
            writer.set_analog_general_scale(gen_scale)
            writer.set_analog_scales(analog_scales)
            writer.set_analog_offsets(analog_offsets)

        if not is_meta_only:
            # Copy frames
            for (i, point, analog) in reader.read_frames(copy=True, camera_sum=False):
                writer.add_frames((point, analog))
        if is_consume:
            # Cleanup
            reader._header = None
            reader._groups = None
            del reader
        return writer

    @property
    def analog_rate(self):
        return super(Writer, self).analog_rate

    @analog_rate.setter
    def analog_rate(self, value):
        per_frame_rate = value / self.point_rate
        assert float(per_frame_rate).is_integer(), "Analog rate must be a multiple of the point rate."
        self._header.analog_per_frame = np.uint16(per_frame_rate)

    @property
    def numeric_key_max(self):
        ''' Get the largest numeric key.
        '''
        num = 0
        if len(self._groups) > 0:
            for i in self._groups.keys():
                if isinstance(i, int):
                    num = max(i, num)
        return num

    @property
    def numeric_key_next(self):
        ''' Get a new unique numeric group key.
        '''
        return self.numeric_key_max + 1

    def get_create(self, label):
        ''' Get or create the specified parameter group.'''
        label = label.upper()
        group = self.get(label)
        if group is None:
            group = self.add_group(self.numeric_key_next, label, label + ' group')
        return group

    @property
    def point_group(self):
        ''' Get or create the POINT parameter group.'''
        return self.get_create('POINT')

    @property
    def analog_group(self):
        ''' Get or create the ANALOG parameter group.'''
        return self.get_create('ANALOG')

    @property
    def trial_group(self):
        ''' Get or create the TRIAL parameter group.'''
        return self.get_create('TRIAL')

    def get(self, group, default=None):
        '''Get a writable group or a parameter instance.

        Parameters
        ----------
        key : str
            Key, see Manager.get() for valid key formats.
        default : any
            Return this value if the named group and parameter are not found.

        Returns
        -------
        value : :class:`GroupWritable` or :class:`ParamWritable`
            Either a decorated group instance or parameter with the specified name(s). If neither
            is found, the default value is returned.
        '''
        return super(Writer, self)._get(group, default)

    def add_group(self, *args, **kwargs):
        '''Add a new parameter group. See Manager.add_group() for more information.

        Returns
        -------
        group : :class:`GroupWritable`
            An editable group instance.
        '''
        return GroupWritable(super(Writer, self)._add_group(*args, **kwargs))

    def rename_group(self, *args):
        ''' Rename a specified parameter group (see Manager._rename_group for args). '''
        self._rename_group(*args)

    def remove_group(self, *args):
        '''Remove the parameter group. (see Manager._rename_group for args). '''
        self._remove_group(*args)

    def add_frames(self, frames, index=None):
        '''Add frames to this writer instance.

        Parameters
        ----------
        frames : Single or sequence of (point, analog) pairs
            A sequence or frame of frame data to add to the writer.
        index : int or None
            Insert the frame or sequence at the index (the first sequence frame will be inserted at give index).
            Note that the index should be relative to 0 rather then the frame number provided by read_frames()!
        '''
        sh = np.shape(frames)
        # Single frame
        if len(sh) != 2:
            frames = [frames]
            sh = np.shape(frames)
        # Sequence of invalid shape
        if sh[1] != 2:
            raise ValueError(
                'Expected frame input to be sequence of point and analog pairs on form (-1, 2). ' +
                '\Input was of shape {}.'.format(str(sh)))

        if index is not None:
            self._frames[index:index] = frames
        else:
            self._frames.extend(frames)

    @staticmethod
    def pack_labels(labels):
        labels = np.ravel(labels)
        # Get longest label name
        label_max_size = 0
        label_max_size = max(label_max_size, np.max([len(label) for label in labels]))
        label_str = ''.join(label.ljust(label_max_size) for label in labels)
        return label_str, label_max_size

    def set_point_labels(self, labels):
        ''' Set point data labels.
        '''
        label_str, label_max_size = Writer.pack_labels(labels)
        self.point_group.add_str('LABELS', 'Point labels.', label_str, label_max_size, len(labels))

    def set_analog_labels(self, labels):
        ''' Set analog data labels.
        '''
        label_str, label_max_size = Writer.pack_labels(labels)
        self.analog_group.add_str('LABELS', 'Analog labels.', label_str, label_max_size, len(labels))

    def set_analog_general_scale(self, value):
        ''' Set ANALOG:GEN_SCALE factor (uniform analog scale factor).
        '''
        self.analog_group.set('GEN_SCALE', 'Analog general scale factor', 4, '<f', value)

    def set_analog_scales(self, values):
        ''' Set ANALOG:SCALE factors (per channel scale factor).

        Parameters
        ----------
        values : iterable or None
            Iterable containing individual scale factors for encoding analog channel data.
        '''
        if is_iterable(values):
            data = np.array([v for v in values], dtype=np.float32)
            self.analog_group.set_array('SCALE', 'Analog channel scale factors', data)
        elif values is None:
            self.analog_group.set_empty_array('SCALE', 'Analog channel scale factors', 4)
        else:
            raise ValueError('Expected iterable containing analog scale factors.')

    def set_analog_offsets(self, values):
        ''' Set ANALOG:OFFSET offsets (per channel offset).

        Parameters
        ----------
        values : iterable or None
            Iterable containing individual offsets for encoding analog channel data.
        '''
        if is_iterable(values):
            data = np.array([v for v in values], dtype=np.int16)
            self.analog_group.set_array('OFFSET', 'Analog channel offsets', data)
        elif values is None:
            self.analog_group.set_empty_array('OFFSET', 'Analog channel offsets', 2)
        else:
            raise ValueError('Expected iterable containing analog data offsets.')

    def set_start_frame(self, frame=1):
        ''' Set the 'TRIAL:ACTUAL_START_FIELD' parameter and header.first_frame entry.

        Parameter
        ---------
        frame : int
            Number for the first frame recorded in the file.
            Frame counter for a trial recording always start at 1 for the first frame.
        '''
        self.trial_group.set('ACTUAL_START_FIELD', 'Actual start frame', 2, '<I', frame, 2)
        if frame < 65535:
            self._header.first_frame = np.uint16(frame)
        else:
            self._header.first_frame = np.uint16(65535)

    def _set_last_frame(self, frame):
        ''' Sets the 'TRIAL:ACTUAL_END_FIELD' parameter and header.last_frame entry.
        '''
        self.trial_group.set('ACTUAL_END_FIELD', 'Actual end frame', 2, '<I', frame, 2)
        self._header.last_frame = np.uint16(min(frame, 65535))


    def set_screen_axis(self, X='+X', Y='+Y'):
        ''' Set the X_SCREEN and Y_SCREEN parameters in the POINT group.

        Parameter
        ---------
        X : str
            2 character string with first character indicating positive or negative axis (+/-),
            and the second axis (X/Y/Z). Examples: '+X' or '-Y'
        Y : str
            Second axis string with same format as Y. Determines the second Y screen axis.
        '''
        if len(X) != 2:
            raise ValueError('Expected string literal to be a 2 character string for the X_SCREEN parameter.')
        if len(Y) != 2:
            raise ValueError('Expected string literal to be a 2 character string for the Y_SCREEN parameter.')
        group = self.point_group
        group.set_str('X_SCREEN', 'X_SCREEN parameter', X, 2)
        group.set_str('Y_SCREEN', 'Y_SCREEN parameter', Y, 2)

    def write(self, handle):
        '''Write metadata and point + analog frames to a file handle.

        Parameters
        ----------
        handle : file
            Write metadata and C3D motion frames to the given file handle. The
            writer does not close the handle.
        '''
        if not self._frames:
            raise RuntimeError('Attempted to write empty file.')

        points, analog = self._frames[0]
        ppf = len(points)
        apf = len(analog)


        first_frame = self.first_frame
        if first_frame <= 0: # Bad value
            first_frame = 1
        nframes = len(self._frames)
        last_frame = first_frame + nframes - 1

        UINT16_MAX = 65535

        # POINT group
        group = self.point_group
        group.set('USED', 'Number of point samples', 2, '<H', ppf)
        group.set('FRAMES', 'Total frame count', 2, '<H', min(UINT16_MAX, nframes))
        if nframes >= UINT16_MAX:
            # Should be floating point
            group.set('LONG_FRAMES', 'Total frame count', 4, '<f', np.float32(nframes))
        elif 'LONG_FRAMES' in group:
            # Docs states it should not exist if frame_count < 65535
            group.remove_param('LONG_FRAMES')
        group.set('DATA_START', 'First data block containing frame samples.', 2, '<H', 0)
        group.set('SCALE', 'Point data scaling factor', 4, '<f', np.float32(self.point_scale))
        group.set('RATE', 'Point data sample rate', 4, '<f', np.float32(self.point_rate))
        # Optional
        if 'UNITS' not in group:
            group.add_str('UNITS', 'Units used for point data measurements.', 'mm', 2)
        if 'DESCRIPTIONS' not in group:
            group.add_str('DESCRIPTIONS', 'Channel descriptions.', ' ' * ppf, 1, ppf)

        # ANALOG group
        group = self.analog_group
        group.set('USED', 'Analog channel count', 2, '<H', apf)
        group.set('RATE', 'Analog samples per second', 4, '<f', np.float32(self.analog_rate))
        if 'GEN_SCALE' not in group:
            self.set_analog_general_scale(1.0)
        # Optional
        if 'SCALE' not in group:
            self.set_analog_scales(None)
        if 'OFFSET' not in group:
            self.set_analog_offsets(None)
        if 'DESCRIPTIONS' not in group:
            group.add_str('DESCRIPTIONS', 'Channel descriptions.', ' ' * apf, 1, apf)

        # TRIAL group
        self.set_start_frame(first_frame)
        self._set_last_frame(last_frame)

        # sync parameter information to header.
        start_block = self.parameter_blocks() + 2
        self.get('POINT:DATA_START').bytes = struct.pack('<H', start_block)
        self._header.data_block = np.uint16(start_block)
        self._header.point_count = np.uint16(ppf)
        self._header.analog_count = np.uint16(np.prod(analog.shape))

        self._write_metadata(handle)
        self._write_frames(handle)

    def _pad_block(self, handle):
        '''Pad the file with 0s to the end of the next block boundary.'''
        extra = handle.tell() % 512
        if extra:
            handle.write(b'\x00' * (512 - extra))

    def _write_metadata(self, handle):
        '''Write metadata to a file handle.

        Parameters
        ----------
        handle : file
            Write metadata and C3D motion frames to the given file handle. The
            writer does not close the handle.
        '''
        self._check_metadata()

        # Header
        self._header.write(handle)
        self._pad_block(handle)
        assert handle.tell() == 512

        # Groups
        handle.write(struct.pack(
            'BBBB', 0, 0, self.parameter_blocks(), self._dtypes.processor))
        for group_id, group in self.listed():
            group._data.write(group_id, handle)

        # Padding
        self._pad_block(handle)
        while handle.tell() != 512 * (self.header.data_block - 1):
            handle.write(b'\x00' * 512)

    def _write_frames(self, handle):
        '''Write our frame data to the given file handle.

        Parameters
        ----------
        handle : file
            Write metadata and C3D motion frames to the given file handle. The
            writer does not close the handle.
        '''
        assert handle.tell() == 512 * (self._header.data_block - 1)
        scale_mag = abs(self.point_scale)
        is_float = self.point_scale < 0
        if is_float:
            point_dtype = self._dtypes.float32
            point_scale = 1.0
        else:
            point_dtype = self._dtypes.int16
            point_scale = scale_mag
        raw = np.zeros((self.point_used, 4), point_dtype)

        analog_scales, analog_offsets = self.get_analog_transform()
        analog_scales_inv = 1.0 / analog_scales

        for points, analog in self._frames:
            # Transform point data
            valid = points[:, 3] >= 0.0
            raw[~valid, 3] = -1
            raw[valid, :3] = points[valid, :3] / point_scale
            raw[valid, 3] = np.bitwise_or(np.rint(points[valid, 3] / scale_mag).astype(np.uint8),
                                          (points[valid, 4].astype(np.uint16) << 8),
                                          dtype=np.uint16)

            # Transform analog data
            analog = analog * analog_scales_inv + analog_offsets
            analog = analog.T

            # Write
            analog = analog.astype(point_dtype)
            handle.write(raw.tobytes())
            handle.write(analog.tobytes())
        self._pad_block(handle)
