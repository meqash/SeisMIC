'''
:copyright:
:license:
   GNU Lesser General Public License, Version 3
   (https://www.gnu.org/copyleft/lesser.html)
:author:
   Peter Makus (makus@gfz-potsdam.de)

Created: Monday, 29th March 2021 07:58:18 am
Last Modified: Thursday, 15th April 2021 09:49:04 am
'''
from copy import deepcopy
import os

from mpi4py import MPI
import numpy as np
from obspy import Stream, UTCDateTime, Trace, Inventory
from obspy.core import Stats
import obspy.signal as osignal
from pyasdf import ASDFDataSet
from scipy.fftpack import next_fast_len
from scipy import signal

# from miic3.utils.nextpowof2 import nextpowerof2
from miic3.utils.miic_utils import trace_calc_az_baz_dist, inv_calc_az_baz_dist
from miic3.db.asdf_handler import get_available_stations, NoiseDB
from miic3.utils.fetch_func_from_str import func_from_str


zerotime = UTCDateTime(1971, 1, 1)


# Note, when using MPI and hdf5 in parallel: All processes that open or write
# to an hdf5 file have to do that in parallel to the same file else MPI will
# freeze

class Correlator(object):
    def __init__(self, options: dict) -> None:
        super().__init__()
        # init MPI
        self.comm = MPI.COMM_WORLD
        self.psize = self.comm.Get_size()
        self.rank = self.comm.Get_rank()
        self.proj_dir = options['proj_dir']
        self.corr_dir = os.path.join(self.proj_dir, options['co']['subdir'])

        self.options = options['co']

        # find the available noise dbs
        network = options['net']['network']
        station = options['net']['station']
        if isinstance(network, str):
            network = [network]
        if isinstance(station, str):
            station = [station]
        if len(station) != len(network):
            if len(station) == 1:
                station = station*len(network)
            elif len(network) == 1:
                network = network*len(station)
            else:
                raise ValueError("""The lists containing network and station
                codes must either have the same length or one of them can have
                the length 1""")
        # Resolve patterns
        self.netlist = []
        self.statlist = []
        for net, stat in zip(network, station):
            if '*' in network+station or '?' in net+stat:
                net, stat = get_available_stations(options['dir'], net, stat)
                self.netlist.extend(net)
                self.statlist.extend(stat)
            else:
                self.netlist.append(net)
                self.statlist.append(stat)

        # and, finally, find the hdf5 files associated to each of them
        self.noisedbs = [NoiseDB(
            os.path.join(self.proj_dir, options['prepro_subdir']), net, stat)
            for net, stat in zip(self.netlist, self.statlist)]
        # note that the indexing in the noisedb list and in the statlist
        # are identical, which could be useful to find them easily
        # Check whether all the data has the same sampling rate
        self.sampling_rate = self.noisedbs[0].sampling_rate
        for ndb in self.noisedbs:
            if ndb.sampling_rate != self.sampling_rate:
                raise ValueError(
                    'Station %s.%s has a different sampling rate with %s Hz.\
                The sampling rate taken from station %s.%s is %s Hz.'
                    % (ndb.network, ndb.station, ndb.sampling_rate,
                        self.noisedbs[0].network, self.noisedbs[0].station,
                        self.sampling_rate))

    def pxcorr(self):
        # Maybe here we have to ensure that all traces have the same length
        # We start out by moving the stream into a matrix
        st = self._all_data_to_stream()
        if self.rank == 0:
            # put all the data into a single stream
            starttime = []
            npts = []
            for tr in st:
                starttime.append(tr.stats['starttime'])
                npts.append(tr.stats['npts'])
            npts = np.max(np.array(npts))
            # create numpy array
            A = st_to_np_array(st)
            As = A.shape
        else:
            starttime = None
            npts = None
            As = None
        # del st  # save some memory
        starttime = self.comm.bcast(starttime, root=0)
        npts = self.comm.bcast(npts, root=0)

        # Tell the other processes the shape of A
        As = self.comm.bcast(As, root=0)
        if self.rank != 0:
            A = np.empty(As, dtype=np.float32)
        self.comm.Bcast([A, MPI.FLOAT], root=0)

        self.options.update(
            {'starttime': starttime,
                'sampling_rate': self.sampling_rate})
        A, starttime = self._pxcorr_matrix(A)

        # put trace into a stream
        cst = Stream()
        # Fetch station coordinates
        inv = self._get_inventory()
        for ii, (startt, comb) in enumerate(
                zip(starttime, self.options['combinations'])):
            cstats = combine_stats(st[comb[0]], st[comb[1]], inv=inv)
            cstats['starttime'] = startt
            cstats['npts'] = npts
            cst.append(Trace(data=A[:, ii], header=cstats))
            cst[-1].stats_tr1 = st[comb[0]].stats
            cst[-1].stats_tr2 = st[comb[1]].stats

        ###############
        # Write correlations to ASDF
        print('creating cstlist')
        # No pretty way of organising, but the only way I got it to work
        # cst.sort()
        # cstlist = []
        # station = cst[0].stats.station
        # network = cst[0].stats.network
        # st = Stream()
        # for tr in cst:
        #     if tr.stats.station == station and tr.stats.network == network:
        #         st.append(cst.pop(0))
        #     else:
        #         cstlist.append(st.copy())
        #         st.clear()
        #         station = tr.stats.station
        #         network = tr.stats.network
        #         st.append(tr)
        cst.sort()
        matrixlist_station = []
        matrixlist_location = []
        datalist = []
        statlist_station = []
        statlist_location = []
        station = cst[0].stats.station
        network = cst[0].stats.network
        loc = cst[0].stats.location
        chan = cst[0].stats.channel
        statlist_location.append(cst[0].stats)
        for tr in cst:
            if tr.stats.station == station and tr.stats.network == network and\
                 tr.stats.location == loc and tr.stats.channel == chan:
                datalist.append(cst.pop(0).data)
            elif tr.stats.station == station and tr.stats.network == network:
                statlist_location.append(tr.stats)
                matrixlist_location.append(
                    np.array(datalist, dtype=np.float32))
                datalist.clear()
                datalist.append(cst.pop(0).data)
                loc = tr.stats.location
                chan = tr.stats.channel
            else:
                matrixlist_location.append(
                    np.array(datalist, dtype=np.float32))
                matrixlist_station.append(matrixlist_location.copy())
                matrixlist_location.clear()
                statlist_station.append(statlist_location.copy())
                statlist_location.clear()
                statlist_location.append(tr.stats)
                station = tr.stats.station
                network = tr.stats.network
                loc = tr.stats.location
                chan = tr.stats.channel
                datalist.clear()
                datalist.append(cst.pop(0).data)
        matrixlist_location.append(
                    np.array(datalist, dtype=np.float32))
        matrixlist_station.append(matrixlist_location)

        # Decide which process writes to which station
        # pmap = (np.arange(len(cstlist))*self.psize)/len(cstlist)
        # pmap = pmap.astype(np.int32)
        # ind = pmap == self.rank
        # ind = np.arange(len(cstlist))[ind]

        n_stats = len(matrixlist_station)
        pmap = (np.arange(n_stats)*self.psize)/n_stats
        pmap = pmap.astype(np.int32)
        ind = pmap == self.rank
        ind = np.arange(n_stats)[ind]

        print('writing files')
        for ii in ind:
            print('write file', ii)
            outf = os.path.join(self.corr_dir, '%s.%s.h5' % (
                    statlist_station[ii][0].stats.network,
                    statlist_station[ii][0].stats.station))
            with ASDFDataSet(outf, mpi=False) as ds:
                for A, stats in zip(
                        matrixlist_station[ii], statlist_station[ii]):
                    ds.add_auxiliary_data(
                        A, 'Correlation', '%s.%s'
                        % (stats.location, stats.channel), parameters=stats)
        # for ii in ind:
        #     print('write file', ii)
        #     outf = os.path.join(self.corr_dir, '%s.%s.h5' % (
        #             cstlist[ii][0].stats.network,
        #             cstlist[ii][0].stats.station))
        #     with ASDFDataSet(outf, mpi=False) as ds:
        #         for tr in cstlist[ii]:
        #             ds.add_auxiliary_data(
        #                 tr.data, 'Correlation', '%s.%s'
        #                 % (cstlist[ii][0].stats.network,
        #                     cstlist[ii][0].stats.station), parameters=tr.stats)

        # return cst

    def _get_inventory(self) -> Inventory:
        inv = Inventory()
        for ndb in self.noisedbs:
            inv.extend(ndb.get_inventory())
        return inv

    def _all_data_to_stream(self) -> Stream:
        """
        Moves all the available data into one `~obspy.core.Stream` object.
        Also computes all the available station combinations from that data.

        :return: [description]
        :rtype: :class:`~obspy.core.stream.Stream`
        """

        st = Stream()
        for ndb in self.noisedbs:
            st.extend(ndb.get_all_data())
        self.options['combinations'] = calc_cross_combis(st)
        return st

    def _pxcorr_matrix(self, A: np.ndarray):
        # time domain processing
        # map of traces on processes
        ntrc = A.shape[1]
        pmap = (np.arange(ntrc)*self.psize)/ntrc
        # This step was not in the original but is necessary for it to work?
        # maybe a difference in an old python/np version?
        pmap = pmap.astype(np.int32)

        # indices for traces to be worked on by each process
        ind = pmap == self.rank

    ######################################
        corr_args = self.options['corr_args']
        # time domain pre-processing
        params = {}
        for key in list(corr_args.keys()):
            if 'Processing' not in key:
                params.update({key: corr_args[key]})
        params['sampling_rate'] = self.sampling_rate
        # This is already done before
        # for proc in self.options['TDpreProcessing']:
        #     A[:, ind] = proc['function'](A[:, ind], proc['args'], params)
        # zero-padding
        A = zeroPadding(A, {'type': 'avoidWrapPowerTwo'}, params)

        ######################################
        # FFT
        # Allocate space for rfft of data
        zmsize = A.shape
        fftsize = zmsize[0]//2+1
        B = np.zeros((fftsize, ntrc), dtype=complex)

        B[:, ind] = np.fft.rfft(A[:, ind], axis=0)
        # B[ind, :] = np.fft.rfft(A[ind, :], axis=1)  # Should axis be =1?
        freqs = rfftfreq(zmsize[0], 1./self.sampling_rate)

        ######################################
        # frequency domain pre-processing
        params.update({'freqs': freqs})
        # Here, I will have to make sure to add all the functions to the module
        for proc in corr_args['FDpreProcessing']:

            # The big advantage of this rather lengthy code is that we can also
            # import any function that has been defined anywhere else (i.e,
            # not only within the miic framework)
            func = func_from_str(proc['function'])
            B[:, ind] = func(B[:, ind], proc['args'], params)

        ######################################
        # collect results
        self.comm.barrier()
        self.comm.Allreduce(MPI.IN_PLACE, [B, MPI.DOUBLE], op=MPI.SUM)

        ######################################
        # correlation
        csize = len(self.options['combinations'])
        irfftsize = (fftsize-1)*2
        sampleToSave = int(
            np.ceil(
                corr_args['lengthToSave'] * self.sampling_rate))
        C = np.zeros((sampleToSave*2+1, csize), dtype=np.float64)

        # center = irfftsize // 2

        pmap = (np.arange(csize)*self.psize)/csize
        pmap = pmap.astype(np.int32)
        ind = pmap == self.rank
        ind = np.arange(csize)[ind]
        starttimes = np.zeros(csize, dtype=np.float64)
        for ii in ind:
            # offset of starttimes in samples(just remove fractions of samples)
            offset = (
                self.options['starttime'][
                    self.options['combinations'][ii][0]] - self.options[
                        'starttime'][self.options['combinations'][ii][1]])
            if corr_args['center_correlation']:
                roffset = 0.
            else:
                # offset exceeding a fraction of integer
                roffset = np.fix(
                    offset * self.sampling_rate) / self.sampling_rate
            # faction of samples to be compenasated by shifting
            offset -= roffset
            # normalization factor of fft correlation
            if corr_args['normalize_correlation']:
                norm = (
                    np.sqrt(
                        2.*np.sum(B[:, self.options[
                            'combinations'][ii][0]] *
                            B[:, self.options['combinations'][ii][0]].conj()) -
                        B[0, self.options['combinations'][ii][0]]**2) *
                    np.sqrt(
                        2.*np.sum(B[:, self.options[
                            'combinations'][ii][1]] *
                            B[:, self.options['combinations'][ii][1]].conj()) -
                        B[0, self.options['combinations'][ii][1]]**2) /
                    irfftsize).real
            else:
                norm = 1.
            M = (
                B[:, self.options['combinations'][ii][0]].conj() *
                B[:, self.options['combinations'][ii][1]] *
                np.exp(1j * freqs * offset * 2 * np.pi))

            ######################################
            # frequency domain postProcessing
            #

            tmp = np.fft.irfft(M, axis=0).real
            # cut the center and do fftshift
            C[:, ii] = np.concatenate(
                (tmp[-sampleToSave:], tmp[:sampleToSave+1]))/norm
            starttimes[ii] = zerotime - sampleToSave / self.sampling_rate \
                - roffset

        ######################################
        # time domain postProcessing

        ######################################
        # collect results
        self.comm.barrier()
        self.comm.Allreduce(MPI.IN_PLACE, [C, MPI.DOUBLE], op=MPI.SUM)
        self.comm.Allreduce(MPI.IN_PLACE, [starttimes, MPI.DOUBLE], op=MPI.SUM)
        return (C, starttimes)


def st_to_np_array(st: Stream) -> np.ndarray:
    st.sort(keys=['npts'])
    # A = np.zeros((st.count(), st[-1].stats.npts), dtype=np.float32)
    A = np.zeros((st[-1].stats.npts, st.count()), dtype=np.float32)
    for ii, tr in enumerate(st):
        # A[ii, :tr.stats.npts] = tr.data
        A[:tr.stats.npts, ii] = tr.data
    return A


def zeroPadding(A: np.ndarray, args: dict, params: dict) -> np.ndarray:
    """
    Append zeros to the traces

    Pad traces with zeros to increase the speed of the Fourier transforms and
    to avoid wrap around effects. Three possibilities for the length of the
    padding can be set in `args['type']`

        -`nextPowerOfTwo`: traces are padded to a length that is the next power \\
            of two from the original length
        -`avoidWrapAround`: depending on length of the trace that is to be used \\
            the padded part is just long enough to avoid wrap around
        -`avoidWrapPowerTwo`: use the next power of two that avoids wrap around

        :Example: ``args = {'type':'avoidWrapPowerTwo'}``

    :type A: numpy.ndarray
    :param A: time series data with time oriented along the first \\
        dimension (columns)
    :type args: dictionary
    :param args: arguments dictionary as described above
    :type params: dictionary
    :param params: not used here

    :rtype: numpy.ndarray
    :return: zero padded time series data
    """
    npts, ntrc = A.shape
    if args['type'] == 'nextPowerOfTwo':
        N = next_fast_len(npts)
    elif args['type'] == 'avoidWrapAround':
        N = npts + params['sampling_rate'] * params['lengthToSave']
    elif args['type'] == 'avoidWrapPowerTwo':
        N = next_fast_len(int(
            npts + params['sampling_rate'] * params['lengthToSave']))
    else:
        raise ValueError("type '%s' of zero padding not implemented" %
                         args['type'])
    A = np.concatenate((A, np.zeros((N-npts, ntrc), dtype=np.float64)), axis=0)
    return A


def rfftfreq(n, d=1.0):
    """
    Return the Discrete Fourier Transform sample frequencies
    (for usage with rfft, irfft).

    The returned float array `f` contains the frequency bin centers in cycles
    per unit of the sample spacing (with zero at the start).  For instance, if
    the sample spacing is in seconds, then the frequency unit is cycles/second.

    Given a window length `n` and a sample spacing `d`::

      f = [0, 1, ...,     n/2-1,     n/2] / (d*n)   if n is even
      f = [0, 1, ..., (n-1)/2-1, (n-1)/2] / (d*n)   if n is odd

    Unlike `fftfreq` (but like `scipy.fftpack.rfftfreq`)
    the Nyquist frequency component is considered to be positive.

    Parameters
    ----------
    n : int
        Window length.
    d : scalar, optional
        Sample spacing (inverse of the sampling rate). Defaults to 1.

    Returns
    -------
    f : ndarray
        Array of length ``n//2 + 1`` containing the sample frequencies.

    Examples
    --------
    >>> signal = np.array([-2, 8, 6, 4, 1, 0, 3, 5, -3, 4], dtype=float)
    >>> fourier = np.fft.rfft(signal)
    >>> n = signal.size
    >>> sample_rate = 100
    >>> freq = np.fft.fftfreq(n, d=1./sample_rate)
    >>> freq
    array([  0.,  10.,  20.,  30.,  40., -50., -40., -30., -20., -10.])
    >>> freq = np.fft.rfftfreq(n, d=1./sample_rate)
    >>> freq
    array([  0.,  10.,  20.,  30.,  40.,  50.])

    """
    if not isinstance(n, int):
        raise ValueError("n should be an integer")
    val = 1.0/(n*d)
    N = n//2 + 1
    results = np.arange(0, N, dtype=int)
    return results * val


def combine_stats(tr1: Trace, tr2: Trace, inv: Inventory = None):
    """ Combine the meta-information of two ObsPy Trace objects

    This function returns a ObsPy :class:`~obspy.core.trace.Stats` object
    obtained combining the two associated with the input Traces.
    Namely ``tr1.stats`` and ``tr2.stats``.

    The fields ['network','station','location','channel'] are combined in
    a ``-`` separated fashion to create a "pseudo" SEED like ``id``.

    For all the others fields, only "common" information are retained: This
    means that only keywords that exist in both dictionaries will be included
    in the resulting one.

    :type tr1: :class:`~obspy.core.trace.Trace`
    :param tr1: First Trace
    :type tr2: :class:`~obspy.core.trace.Trace`
    :param tr2: Second Trace
    :type inv: :class:`~obspy.core.inventory.Inventory`, optional
    :param inv: Inventory containing the station coordinates. Only needed if
        station coordinates are not in Trace.Stats. Defaults to None.

    :rtype: :class:`~obspy.core.trace.Stats`
    :return: **stats**: combined Stats object
    """

    if not isinstance(tr1, Trace):
        raise TypeError("tr1 must be an obspy Trace object.")

    if not isinstance(tr2, Trace):
        raise TypeError("tr2 must be an obspy Trace object.")

    tr1_keys = list(tr1.stats.keys())
    tr2_keys = list(tr2.stats.keys())

    stats = Stats()

    # Adjust the information to create a new SEED like id
    keywords = ['network', 'station', 'location', 'channel']
    sac_keywords = ['sac']

    for key in keywords:
        if key in tr1_keys and key in tr2_keys:
            stats[key] = tr1.stats[key] + '-' + tr2.stats[key]

    for key in tr1_keys:
        if key not in keywords and key not in sac_keywords:
            if key in tr2_keys:
                if tr1.stats[key] == tr2.stats[key]:
                    # in the stats object there are read only objects
                    try:
                        stats[key] = tr1.stats[key]
                    except AttributeError:
                        pass

    try:
        stats['sac'] = {}
        stats.sac['stla'] = tr1.stats.sac.stla
        stats.sac['stlo'] = tr1.stats.sac.stlo
        stats.sac['stel'] = tr1.stats.sac.stel
        stats.sac['evla'] = tr2.stats.sac.stla
        stats.sac['evlo'] = tr2.stats.sac.stlo
        stats.sac['evel'] = tr2.stats.sac.stel

        az, baz, dist = trace_calc_az_baz_dist(tr1, tr2)

        stats.sac['dist'] = dist / 1000
        stats.sac['az'] = az
        stats.sac['baz'] = baz
    except AttributeError:
        if inv:
            inv1 = inv.select(
                network=tr1.stats.network, station=tr1.stats.station)
            inv2 = inv.select(
                network=tr2.stats.network, station=tr2.stats.station)
            stats['sac'] = {}
            stats.sac['stla'] = inv1[0][0].latitude
            stats.sac['stlo'] = inv1[0][0].longitude
            stats.sac['stel'] = inv1[0][0].elevation
            stats.sac['evla'] = inv2[0][0].latitude
            stats.sac['evlo'] = inv2[0][0].longitude
            stats.sac['evel'] = inv2[0][0].elevation

            az, baz, dist = inv_calc_az_baz_dist(inv1, inv2)

            stats.sac['dist'] = dist / 1000
            stats.sac['az'] = az
            stats.sac['baz'] = baz
        else:
            stats.pop('sac')
            print("No station coordinates provided.")

    return stats


def calc_cross_combis(st, method='betweenStations'):
    """
    Calculate a list of all cross correlation combination
    of traces in the stream: i.e. all combination with two different
    stations involved.

    :type method: string
    :param method: Determines which traces of the strem are combined.

        ``'betweenStations'``: Traces are combined if either their station or
            their network names are different.
        ``'betweenComponents'``: Traces are combined if their components (last
            letter of channel name) names are different and their station and
            network names are identical (single station cross-correlation).
        ``'autoComponents'``: Traces are combined only with themselves.
        ``'allSimpleCombinations'``: All Traces are combined once (onle one of
            (0,1) and (1,0))
        ``'allCombinations'``: All traces are combined in both orders ((0,1)
            and (1,0))
    """

    combis = []
    if method == 'betweenStations':
        for ii, tr in enumerate(st):
            for jj in range(ii+1, len(st)):
                if ((tr.stats['network'] != st[jj].stats['network']) or
                        (tr.stats['station'] != st[jj].stats['station'])):
                    combis.append((ii, jj))
    elif method == 'betweenComponents':
        for ii, tr in enumerate(st):
            for jj in range(ii+1, len(st)):
                if ((tr.stats['network'] == st[jj].stats['network']) and
                    (tr.stats['station'] == st[jj].stats['station']) and
                        (tr.stats['channel'][-1] !=
                            st[jj].stats['channel'][-1])):
                    combis.append((ii, jj))
    elif method == 'autoComponents':
        for ii, _ in enumerate(st):
            combis.append((ii, ii))
    elif method == 'allSimpleCombinations':
        for ii, _ in enumerate(st):
            for jj in range(ii, len(st)):
                combis.append((ii, jj))
    elif method == 'allCombinations':
        for ii, _ in enumerate(st):
            for jj, _ in enumerate(st):
                combis.append((ii, jj))
    else:
        raise ValueError("Method has to be one of ('betweenStations', "
                         "'betweenComponents', 'autoComponents', "
                         "'allSimpleCombinations' or 'allCombinations').")
    return combis


def rotate_multi_corr_stream(st: Stream) -> Stream:
    """Rotate a stream with full Greens tensor from ENZ to RTZ

    Take a stream with numerous correlation traces and rotate the
    combinations of ENZ components into combinations of RTZ components in case
    all nine components of the Green's tensor are present. If not all nine
    components are present no trace for this station combination is returned.

    :type st: obspy.stream
    :param st: stream with data in ENZ system
    :rtype: obspy.stream
    :return: stream in the RTZ system
    """

    out_st = Stream()
    while st:
        tl = list(range(9))
        tst = st.select(network=st[0].stats['network'],
                        station=st[0].stats['station'])
        cnt = 0
        for ttr in tst:
            if ttr.stats['channel'][2] == 'E':
                if ttr.stats['channel'][6] == 'E':
                    tl[0] = ttr
                    cnt += 1
                elif ttr.stats['channel'][6] == 'N':
                    tl[1] = ttr
                    cnt += 2
                elif ttr.stats['channel'][6] == 'Z':
                    tl[2] = ttr
                    cnt += 4
            elif ttr.stats['channel'][2] == 'N':
                if ttr.stats['channel'][6] == 'E':
                    tl[3] = ttr
                    cnt += 8
                elif ttr.stats['channel'][6] == 'N':
                    tl[4] = ttr
                    cnt += 16
                elif ttr.stats['channel'][6] == 'Z':
                    tl[5] = ttr
                    cnt += 32
            elif ttr.stats['channel'][2] == 'Z':
                if ttr.stats['channel'][6] == 'E':
                    tl[6] = ttr
                    cnt += 64
                elif ttr.stats['channel'][6] == 'N':
                    tl[7] = ttr
                    cnt += 128
                elif ttr.stats['channel'][6] == 'Z':
                    tl[8] = ttr
                    cnt += 256
        if cnt == 2**9-1:
            st0 = Stream()
            for t in tl:
                st0.append(t)
            st1 = _rotate_corr_stream(st0)
            out_st += st1
        elif cnt == 27:  # only horizontal component combinations present
            st0 = Stream()
            for t in [0, 1, 3, 4]:
                st0.append(tl[t])
            st1 = _rotate_corr_stream_horizontal(st0)
            out_st += st1
        elif cnt == 283:  # horizontal combinations + ZZ
            st0 = Stream()
            for t in [0, 1, 3, 4]:
                st0.append(tl[t])
            st1 = _rotate_corr_stream_horizontal(st0)
            out_st += st1
            out_st.append(tl[8])
        for ttr in tst:
            for ind, tr in enumerate(st):
                if ttr.id == tr.id:
                    st.pop(ind)

    return out_st


def _rotate_corr_stream_horizontal(st):
    """ Rotate traces in stream from the EE-EN-NE-NN system to
    the RR-RT-TR-TT system. The letters give the component order
    in the input and output streams. Input traces are assumed to be of same
    length and simultaneously sampled.
    """

    # rotation angles
    # phi1 : counter clockwise angle between E and R(towards second station)
    # the leading -1 accounts fact that we rotate the coordinate system,
    # not a vector
    phi1 = - np.pi/180*(90-st[0].stats['sac']['az'])
    # phi2 : counter clockwise angle between E and R(away from first station)
    phi2 = - np.pi/180*(90-st[0].stats['sac']['baz']+180)

    c1 = np.cos(phi1)
    s1 = np.sin(phi1)
    c2 = np.cos(phi2)
    s2 = np.sin(phi2)

    rt = Stream()
    RR = st[0].copy()
    RR.data = c1*c2*st[0].data - c1*s2*st[1].data - s1*c2*st[2].data +\
        s1*s2*st[3].data
    tcha = list(RR.stats['channel'])
    tcha[2] = 'R'
    tcha[6] = 'R'
    RR.stats['channel'] = ''.join(tcha)
    rt.append(RR)

    RT = st[0].copy()
    RT.data = c1*s2*st[0].data + c1*c2*st[1].data - s1*s2*st[2].data -\
        s1*c2*st[3].data
    tcha = list(RT.stats['channel'])
    tcha[2] = 'R'
    tcha[6] = 'T'
    RT.stats['channel'] = ''.join(tcha)
    rt.append(RT)

    TR = st[0].copy()
    TR.data = s1*c2*st[0].data - s1*s2*st[1].data + c1*c2*st[2].data -\
        c1*s2*st[3].data
    tcha = list(TR.stats['channel'])
    tcha[2] = 'T'
    tcha[6] = 'R'
    TR.stats['channel'] = ''.join(tcha)
    rt.append(TR)

    TT = st[0].copy()
    TT.data = s1*s2*st[0].data + s1*c2*st[1].data + c1*s2*st[2].data +\
        c1*c2*st[3].data
    tcha = list(TT.stats['channel'])
    tcha[2] = 'T'
    tcha[6] = 'T'
    TT.stats['channel'] = ''.join(tcha)
    rt.append(TT)

    return rt


def _rotate_corr_stream(st):
    """ Rotate traces in stream from the EE-EN-EZ-NE-NN-NZ-ZE-ZN-ZZ system to
    the RR-RT-RZ-TR-TT-TZ-ZR-ZT-ZZ system. The letters give the component order
    in the input and output streams. Input traces are assumed to be of same
    length and simultaneously sampled.
    """

    # rotation angles
    # phi1 : counter clockwise angle between E and R(towards second station)
    # the leading -1 accounts fact that we rotate the coordinate system,
    # not a vector
    phi1 = - np.pi/180*(90-st[0].stats['sac']['az'])
    # phi2 : counter clockwise angle between E and R(away from first station)
    phi2 = - np.pi/180*(90-st[0].stats['sac']['baz']+180)

    c1 = np.cos(phi1)
    s1 = np.sin(phi1)
    c2 = np.cos(phi2)
    s2 = np.sin(phi2)

    rtz = Stream()
    RR = st[0].copy()
    RR.data = c1*c2*st[0].data - c1*s2*st[1].data - s1*c2*st[3].data +\
        s1*s2*st[4].data
    tcha = list(RR.stats['channel'])
    tcha[2] = 'R'
    tcha[6] = 'R'
    RR.stats['channel'] = ''.join(tcha)
    rtz.append(RR)

    RT = st[0].copy()
    RT.data = c1*s2*st[0].data + c1*c2*st[1].data - s1*s2*st[3].data -\
        s1*c2*st[4].data
    tcha = list(RT.stats['channel'])
    tcha[2] = 'R'
    tcha[6] = 'T'
    RT.stats['channel'] = ''.join(tcha)
    rtz.append(RT)

    RZ = st[0].copy()
    RZ.data = c1*st[2].data - s1*st[5].data
    tcha = list(RZ.stats['channel'])
    tcha[2] = 'R'
    tcha[6] = 'Z'
    RZ.stats['channel'] = ''.join(tcha)
    rtz.append(RZ)

    TR = st[0].copy()
    TR.data = s1*c2*st[0].data - s1*s2*st[1].data + c1*c2*st[3].data -\
        c1*s2*st[4].data
    tcha = list(TR.stats['channel'])
    tcha[2] = 'T'
    tcha[6] = 'R'
    TR.stats['channel'] = ''.join(tcha)
    rtz.append(TR)

    TT = st[0].copy()
    TT.data = s1*s2*st[0].data + s1*c2*st[1].data + c1*s2*st[3].data +\
        c1*c2*st[4].data
    tcha = list(TT.stats['channel'])
    tcha[2] = 'T'
    tcha[6] = 'T'
    TT.stats['channel'] = ''.join(tcha)
    rtz.append(TT)

    TZ = st[0].copy()
    TZ.data = s1*st[2].data + c1*st[5].data
    tcha = list(TZ.stats['channel'])
    tcha[2] = 'T'
    tcha[6] = 'Z'
    TZ.stats['channel'] = ''.join(tcha)
    rtz.append(TZ)

    ZR = st[0].copy()
    ZR.data = c2*st[6].data - s2*st[7].data
    tcha = list(ZR.stats['channel'])
    tcha[2] = 'Z'
    tcha[6] = 'R'
    ZR.stats['channel'] = ''.join(tcha)
    rtz.append(ZR)

    ZT = st[0].copy()
    ZT.data = s2*st[6].data + c2*st[7].data
    tcha = list(ZT.stats['channel'])
    tcha[2] = 'Z'
    tcha[6] = 'T'
    ZT.stats['channel'] = ''.join(tcha)
    rtz.append(ZT)

    rtz.append(st[8].copy())

    return rtz


def set_sample_options() -> dict:
    args = {'TDpreProcessing': [{'function': detrend,
                                'args': {'type': 'linear'}},
                                {'function': taper,
                                'args': {'type': 'cosine_taper', 'p': 0.01}},
                                {'function': mute,
                                'args': {'filter': {
                                    'type': 'bandpass',
                                    'freqmin': 1., 'freqmax': 6.},
                                    'taper_len': 1., 'std_factor': 1,
                                    'extend_gaps': True}},
                                {'function': TDfilter,
                                'args': {
                                    'type': 'bandpass',
                                    'freqmin': 1., 'freqmax': 3.}},
                                {'function': TDnormalization,
                                'args': {'filter': {
                                    'type': 'bandpass', 'freqmin': 0.5,
                                    'freqmax': 2.}, 'windowLength': 1.}},
                                {'function': signBitNormalization,
                                'args': {}}
                                 ],
            'FDpreProcessing': [{'function': spectralWhitening,
                                'args': {}},
                               {'function': FDfilter,
                                'args': {'flimit': [0.5, 1., 5., 7.]}}],
            'lengthToSave': 20,
            'center_correlation': True,
            # make sure zero correlation time is in the center
            'normalize_correlation': True,
            'combinations': [(0, 0), (0, 1), (0, 2), (1, 2)]}

    return args


def spectralWhitening(B: np.ndarray, args: dict, params) -> np.ndarray:
    """
    Spectal whitening of Fourier-transformed date

    Normalize the amplitude spectrum of the complex spectra in `B`. The
    `args` dictionary may contain the keyword `joint_norm`. If its value is
    True the normalization of sets of three traces are normalized jointly by
    the mean of their amplitude spectra. This is useful for later rotation of
    correlated traces in the ZNE system into the ZRT system.

    :type B: numpy.ndarray
    :param B: Fourier transformed time series data with frequency oriented\\
        along the first dimension (columns)
    :type args: dictionary
    :param args: arguments dictionary as described above
    :type params: dictionary
    :param params: not used here

    :rtype: numpy.ndarray
    :return: whitened spectal data
    """
    absB = np.absolute(B)
    if 'joint_norm' in list(args.keys()):
        if args['joint_norm']:
            assert B.shape[1] % 3 == 0, "for joint normalization the number\
                      of traces needs to the multiple of 3: %d" % B.shape[1]
            for ii in np.arange(0, B.shape[1], 3):
                absB[:, ii:ii+3] = np.tile(
                    np.atleast_2d(np.mean(absB[:, ii:ii+3], axis=1)).T, [1, 3])
    with np.errstate(invalid='raise'):
        try:
            B /= absB
        except FloatingPointError as e:
            errargs = np.argwhere(absB == 0)
            # Report error where there is zero divides for a non-zero freq
            if not np.all(errargs[:, 0] == 0):
                print(e)
                print(errargs)

    # Set zero frequency component to zero
    B[0, :] = 0.j

    return B


def FDfilter(B: np.ndarray, args: dict, params: dict) -> np.ndarray:
    """
    Filter Fourier-transformed data

    Filter Fourier tranformed data by tapering in frequency domain. The `args`
    dictionary is supposed to contain the key `flimit` with a value that is a
    four element list or tuple defines the corner frequencies (f1, f2, f3, f4)
    in Hz of the cosine taper which is one between f2 and f3 and tapers to zero
    for f1 < f < f2 and f3 < f < f4.

    :type B: numpy.ndarray
    :param B: Fourier transformed time series data with frequency oriented\\
        along the first dimension (columns)
    :type args: dictionary
    :param args: arguments dictionary as described above
    :type params: dictionary
    :param params: params['freqs'] contains an array with the freqency values
        of the samples in `B`

    :rtype: numpy.ndarray
    :return: filtered spectal data
    """
    args = deepcopy(args)
    args.update({'freqs': params['freqs']})
    tap = osignal.invsim.cosine_taper(B.shape[0], **args)
    B *= np.tile(np.atleast_2d(tap).T, (1, B.shape[1]))
    return B


def FDsignBitNormalization(
        B: np.ndarray, args: dict, params: dict) -> np.ndarray:
    """
    Sign bit normalization of frequency transformed data

    Divides each sample by its amplitude resulting in trace with amplidues of
    (-1, 0, 1). As this operation is done in frequency domain it requires two
    Fourier transforms and is thus quite costly but alows to be performed
    after other steps of frequency domain procesing e.g. whitening.


    :type B: numpy.ndarray
    :param B: Fourier transformed time series data with frequency oriented\\
        along the first dimension (columns)
    :type args: dictionary
    :param args: not used in this function
    :type params: dictionary
    :param params: not used in this function

    :rtype: numpy.ndarray
    :return: frequency transform of the 1-bit normalized data
    """
    B = np.fft.irfft(B, axis=0)
    C = B.real
    C = np.sign(B)
    return np.fft.rfft(C, axis=0)


def mute(A: np.ndarray, args: dict, params: dict) -> np.ndarray:
    """
    Mute parts of data that exceed a threshold

    To completely surpress the effect of data with high amplitudes e.g. after
    aftershocks these parts of the data are muted (set to zero). The respective
    parts of the signal are identified as those where the envelope in a given
    frequency exceeds a threshold given directly as absolute numer or as a
    multiple of the data's standard deviation. A taper of length `taper_len` is
    applied to smooth the edges of muted segments. Setting `extend_gaps` to
    Ture will ensure that the taper is applied outside the segments and data
    inside these segments will all zero. Edges of the data will be tapered too
    in this case.

    :Example:
    ``args={'filter':{'type':'bandpass', 'freqmin':1., 'freqmax':6.},'taper_len':1., 'threshold':1000, 'std_factor':1, 'extend_gaps':True}``

    :type A: numpy.ndarray
    :param A: time series data with time oriented along the first dimension
        (columns)
    :type args: dictionary
    :param args: the following keywords are allowed:

        * `filter`: (dictionary) description of filter to be applied before
            calculation of the signal envelope. If not given the envelope is
            calculated from raw data. The value of the keyword filter is the
            same as the `args` for the function `TDfilter`.
        * `threshold`: (float) absolute amplitude of threhold for muting
        * `std_factor`: (float) alternativly to an absolute number the threhold
            can be estimated as a multiple of the standard deviation if the
            scaling is given in as value of the keyword `std_factor`. If
            neither `threshold` nor `std_factor` are given `std_factor`=1 is
            assumed.
        * `extend_gaps` (boolean) if True date abive the threshold is
            guaranteed to be muted, otherwise tapering will leak into these
            parts. This step involves an additional convolution.
        * `taper_len`: (float) length of taper for muted segments in seconds
    :type params: dictionary
    :param params: filled automatically by `pxcorr`

    :rtype: numpy.ndarray
    :return: clipped time series data
    """

    # return zeros if length of traces is shorter than taper
    ntap = int(args['taper_len']*params['sampling_rate'])
    if A.shape[0] <= ntap:
        return np.zeros_like(A)

    # filter if asked to
    if 'filter' in list(args.keys()):
        C = TDfilter(A, args['filter'], params)
    else:
        C = deepcopy(A)

    # calculate envelope
    D = np.abs(C)

    # calculate threshold
    if 'threshold' in list(args.keys()):
        thres = np.zeros(A.shape[1]) + args['threshold']
    elif 'std_factor' in list(args.keys()):
        thres = np.std(C, axis=0) * args['std_factor']
    else:
        thres = np.std(C, axis=0)

    # calculate mask
    mask = np.ones_like(D)
    mask[D > np.tile(np.atleast_2d(thres), (A.shape[0], 1))] = 0
    # extend the muted segments to make sure the whole segment is zero after
    if args['extend_gaps']:
        tap = np.ones(ntap)/ntap
        for ind in range(A.shape[1]):
            mask[:, ind] = np.convolve(mask[:, ind], tap, mode='same')
        nmask = np.ones_like(D)
        nmask[mask < 1.] = 0
    else:
        nmask = mask

    # apply taper
    tap = 2. - (np.cos(np.arange(ntap, dtype=float)/ntap*2.*np.pi) + 1.)
    tap /= ntap
    for ind in range(A.shape[1]):
        nmask[:, ind] = np.convolve(nmask[:, ind], tap, mode='same')

    # mute date with tapered mask
    A *= nmask
    return A


def TDfilter(A: np.ndarray, args: dict, params: dict) -> np.ndarray:
    """
    Filter time series data

    Filter in time domain. Types of filters are defined by `obspy.signal`.

    `args` has the following structure:

        args = {'type':`filterType`, fargs}

        `type` may be `bandpass` with the corresponding fargs `freqmin` and
        `freqmax` or `highpass`/`lowpass` with the `fargs` `freqmin`/`freqmax`

        :Example:
        ``args = {'type':'bandpass','freqmin':0.5,'freqmax':2.}``

    :type A: numpy.ndarray
    :param A: time series data with time oriented along the first \\
        dimension (columns)
    :type args: dictionary
    :param args: arguments dictionary as described above
    :type params: dictionary
    :param params: not used here

    :rtype: numpy.ndarray
    :return: filtered time series data
    """
    func = getattr(osignal.filter, args['type'])
    args = deepcopy(args)
    args.pop('type')
    # filtering in obspy.signal is done along the last dimension that why .T
    A = func(A.T, df=params['sampling_rate'], **args).T
    return A


def normalizeStandardDeviation(
        A: np.ndarray, args: dict, params: dict) -> np.ndarray:
    """
    Divide the time series by their standard deviation

    Divide the amplitudes of each trace by its standard deviation.

    :type A: numpy.ndarray
    :param A: time series data with time oriented along the first \\
        dimension (columns)
    :type args: dictionary
    :param args: not used here
    :type params: dictionary
    :param params: not used here

    :rtype: numpy.ndarray
    :return: normalized time series data
    """
    std = np.std(A, axis=0)
    A /= np.tile(std, (A.shape[0], 1))
    return A


def signBitNormalization(
        A: np.ndarray, args: dict, params: dict) -> np.ndarray:
    """
    One bit normalization of time series data

    Return the sign of the samples (-1, 0, 1).

    :type A: numpy.ndarray
    :param A: time series data with time oriented along the first \\
        dimension (columns)
    :type args: dictionary
    :param args: not used here
    :type params: dictionary
    :param params: not used here

    :rtype: numpy.ndarray
    :return: 1-bit normalized time series data
    """
    return np.sign(A)


def detrend(A: np.ndarray, args: dict, params: dict) -> np.ndarray:
    """
    Remove trend from data

    Remove the trend from the time series data in `A`. Several methods are \\
    possible. The method is specified as the value of the `type` keyword in
    the argument dictionary `args`.

    Possible `types` of detrending:
       -`constant` or `demean`: substract mean of traces

       -`linear`: substract a least squares fitted linear trend form the data

    :type A: numpy.ndarray
    :param A: time series data with time oriented along the first \\
        dimension (columns)
    :type args: dictionary
    :param args: the only used keyword is `type`
    :type params: dictionary
    :param params: not used here

    :rtype: numpy.ndarray
    :return: detrended time series data
    """
    # for compatibility with obspy
    if args['type'] == 'demean':
        args['type'] = 'constant'
    if args['type'] == 'detrend':
        args['type'] = 'linear'

    # Detrend function taken from scipy and modified to account for nan
    type = args['type']
    axis = 0
    data = A
    if type not in ['linear', 'l', 'constant', 'c']:
        raise ValueError("Trend type must be 'linear' or 'constant'.")
    data = np.asarray(data)
    dtype = data.dtype.char
    if dtype not in 'dfDF':
        dtype = 'd'
    if type in ['constant', 'c']:
        ret = data - np.expand_dims(np.nanmean(data, axis), axis)
        return ret
    else:
        dshape = data.shape
        N = dshape[axis]
        bp = np.sort(np.unique(r_[0, bp, N]))
        if np.any(bp > N):
            raise ValueError("Breakpoints must be less than length "
                             "of data along given axis.")
        Nreg = len(bp) - 1
        # Restructure data so that axis is along first dimension and
        #  all other dimensions are collapsed into second dimension
        rnk = len(dshape)
        if axis < 0:
            axis = axis + rnk
        newdims = r_[axis, 0:axis, axis + 1:rnk]
        newdata = np.reshape(np.transpose(data, tuple(newdims)),
                          (N, _prod(dshape) // N))
        newdata = newdata.copy()  # make sure we have a copy
        if newdata.dtype.char not in 'dfDF':
            newdata = newdata.astype(dtype)
        # Find leastsq fit and remove it for each piece
        for m in range(Nreg):
            Npts = bp[m + 1] - bp[m]
            A = np.ones((Npts, 2), dtype)
            A[:, 0] = np.cast[dtype](np.arange(1, Npts + 1) * 1.0 / Npts)
            sl = slice(bp[m], bp[m + 1])
            coef, resids, rank, s = np.linalg.lstsq(A, newdata[sl])
            newdata[sl] = newdata[sl] - np.dot(A, coef)
        # Put data back in original shape.
        tdshape = np.take(dshape, newdims, 0)
        ret = np.reshape(newdata, tuple(tdshape))
        vals = list(range(1, rnk))
        olddims = vals[:axis] + [0] + vals[axis:]
        ret = np.transpose(ret, tuple(olddims))
        return ret


def TDnormalization(A: np.ndarray, args: dict, params: dict) -> np.ndarray:
    """
    Amplitude dependent time domain normalization

    Calculate the envelope of the filtered trace, smooth it in a window of
    length `windowLength` and normalize the waveform by this trace. The two
    used keywords in `args` are `filter and `windowLength` that describe the
    filter and the length of the envelope smoothing window, respectively.

    `args` has the following structure:

        args = {'windowLength':`length of the envelope smoothing window in \\
        [s]`,'filter':{'type':`filterType`, fargs}}``

        `type` may be `bandpass` with the corresponding fargs `freqmin` and \\
        `freqmax` or `highpass`/`lowpass` with the `fargs` `freqmin`/`freqmax`

        :Example:
        ``args = {'windowLength':5,'filter':{'type':'bandpass','freqmin':0.5,
        'freqmax':2.}}``

    :type A: numpy.ndarray
    :param A: time series data with time oriented along the first \\
        dimension (columns)
    :type args: dictionary
    :param args: arguments dictionary as described above
    :type params: dictionary
    :param params: not used here

    :rtype: numpy.ndarray
    :return: normalized time series data
    """
    # filter if args['filter']
    B = deepcopy(A)
    if args['filter']:
        func = getattr(osignal, args['filter']['type'])
        fargs = deepcopy(args['filter'])
        fargs.pop('type')
        B = func(A.T, df=params['sampling_rate'], **fargs).T
    else:
        B = deepcopy(A)
    # simple calculation of envelope
    B = B**2
    # print 'shape B', B.shape
    # smoothing of envelepe in both directions to avoid a shift
    window = (
        np.ones(np.ceil(args['windowLength'] * params['sampling_rate']))
        / np.ceil(args['windowLength']*params['sampling_rate']))
    # print 'shape window', window.shape
    for ind in range(B.shape[1]):
        B[:, ind] = np.convolve(B[:, ind], window, mode='same')
        B[:, ind] = np.convolve(B[::-1, ind], window, mode='same')[::-1]
        B[:, ind] += np.max(B[:, ind])*1e-6
    # normalization
    A /= np.sqrt(B)
    return A


def taper(A: np.ndarray, args: dict, params: dict) -> np.ndarray:
    """
    Taper to the time series data

    Apply a simple taper to the time series data.

    `args` has the following structure:

        args = {'type':`type of taper`,taper_args}``

        `type` may be `cosine_taper` with the corresponding taper_args `p` the
        percentage of the traces to taper. Possibilities of `type` are \\
        given by `obspy.signal`.

        :Example:
        ``args = {'type':'cosine_taper','p':0.1}``

    :type A: numpy.ndarray
    :param A: time series data with time oriented along the first \\
        dimension (columns)
    :type args: dictionary
    :param args: arguments dictionary as described above
    :type params: dictionary
    :param params: not used here

    :rtype: numpy.ndarray
    :return: tapered time series data
    """
    if args['type'] == 'cosine_taper':
        func = osignal.invsim.cosine_taper
    else:
        func = getattr(signal, args['type'])
    args = deepcopy(args)
    args.pop('type')
    tap = func(A.shape[0], **args)
    A *= np.tile(np.atleast_2d(tap).T, (1, A.shape[1]))
    return A


def clip(A: np.ndarray, args: dict, params: dict) -> np.ndarray:
    """
    Clip time series data at a multiple of the standard deviation

    Set amplitudes exeeding a certatin threshold to this threshold.
    The threshold for clipping is estimated as the standard deviation of each
    trace times a factor specified in `args`.

    :Note: Traces should be demeaned before clipping.

    :type A: numpy.ndarray
    :param A: time series data with time oriented along the first \\
        dimension (columns)
    :type args: dictionary
    :param args: the only keyword allowed is `std_factor` describing the \\
        scaling of the standard deviation for the clipping threshold
    :type params: dictionary
    :param params: not used here

    :rtype: numpy.ndarray
    :return: clipped time series data
    """
    stds = np.nanstd(A, axis=0)
    for ind in range(A.shape[1]):
        ts = args['std_factor']*stds[ind]
        A[A[:, ind] > ts, ind] = ts
        A[A[:, ind] < -ts, ind] = -ts
    return A
