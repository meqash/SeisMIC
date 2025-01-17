'''
:copyright:
    The SeisMIC development team (makus@gfz-potsdam.de).
:license:
    GNU Lesser General Public License, Version 3
    (https://www.gnu.org/copyleft/lesser.html)
:author:
   Peter Makus (makus@gfz-potsdam.de)

Created: Friday, 16th July 2021 02:30:02 pm
Last Modified: Monday, 4th March 2024 02:44:41 pm
'''

from datetime import datetime
from typing import Tuple, List
import os
import locale

import matplotlib as mpl
from matplotlib import pyplot as plt
from matplotlib import dates as mdates
import numpy as np
from obspy import UTCDateTime

from seismic.plot.plot_utils import set_mpl_params


def plot_fancy_dv(
    dv, xlim: Tuple[datetime, datetime] = None,
    ylim: Tuple[float, float] = None, return_ax: bool = False,
    title: str = None, dateformat: str = '%d %b %y', ax: plt.axes = None,
        *args, **kwargs):
    """ Prettier than the technical plot, but less informative"""
    set_mpl_params()
    if not ax:
        fig = plt.figure(figsize=(12, 8))
    else:
        fig = ax.get_figure()

    val = -dv.value[~np.isnan(dv.corr)]*100
    corr_starts = np.array(dv.stats.corr_start)
    t_real = [t.datetime for t in corr_starts[~np.isnan(dv.corr)]]

    ax = ax or plt.gca()

    # plot dv/v
    map = ax.scatter(
        t_real, val, c=dv.corr[~np.isnan(dv.corr)], cmap='inferno_r', s=22)

    # Correct format of X-Axis
    ax.set_ylim(ylim)
    plt.ylabel(r'$\frac{dv}{v}$ [%]')
    plt.grid(True, axis='y')
    try:
        locale.setlocale(locale.LC_ALL, "en_GB.utf8")
    except locale.Error:  # for Mac
        locale.setlocale(locale.LC_ALL, "en_GB.utf-8")
    ax.xaxis.set_major_formatter(mdates.DateFormatter(dateformat))
    plt.xticks(rotation=25)

    fig.colorbar(
        map, orientation='horizontal', pad=0.1, shrink=.6,
        label='Correlation Coefficient', anchor=(0.5, .8))
    plt.title(title)
    if xlim is not None:
        plt.xlim(xlim)
    else:
        plt.xlim((min(t_real), max(t_real)))
    if return_ax:
        return fig, ax


def plot_technical_dv(
    dv, save_dir='.', figure_file_name=None, mark_time=None,
    normalize_simmat=False, sim_mat_Clim=[], figsize=(9, 11), dpi=72,
    ylim: Tuple[float, float] = None, xlim: Tuple[datetime, datetime] = None,
    title: str = None, plot_scatter: bool = False,
    return_ax: bool = False, *args,
        **kwargs) -> Tuple[plt.figure, List[plt.axis]]:
    """
    Plot a :class:`seismic.monitor.dv.DV` object
    The produced figure is saved in `save_dir` that, if necessary, it is
    created.
    It is also possible to pass a "special" time value `mark_time` that will be
    represented in the `dv/v` and `corr` plot as a vertical line; It can be
    a string (i.e. YYYY-MM-DD) or directly a :class:`~datetime.datetime`
    object.
    if the `dv` dictionary also contains a 'comb_mseedid' keyword, its `value`
    (i.e. MUST be a string) will be reported in the title.
    In case of the chosen filename exist in the `save_dir` directory, a prefix
    _<n> with n:0..+Inf, is added.
    The aspect of the plot may change depending on the matplotlib version. The
    recommended one is matplotlib 1.1.1

    :type dv: dict
    :param dv: velocity change estimate dictionary as output by
        :class:`~miic.core.stretch_mod.multi_ref_vchange_and_align` and
        successively "extended" to contain also the timing in the form
        {'time': time_vect} where `time_vect` is a :class:`~numpy.ndarray` of
        :class:`~datetime.datetime` objects.
    :type save_dir: string
    :param save_dir: Directory where to save the produced figure. It is created
        if it doesn't exist.
    :type figure_file_name: string
    :param figure_file_name: filename to use for saving the figure. If None
        the figure is displayed in interactive mode.
    :type mark_time: string or :class:`~datetime.datetime` object
    :param mark_time: It is a "special" time location that will be represented
        in the `dv/v` and `corr` plot as a vertical line.
    :type normalize_simmat: Bool
    :param normalize_simmat: if True the simmat will be normalized to a maximum
        of 1. Defaults to False
    :type sim_mat_Clim: 2 element array_like
    :param sim_mat_Clim: if non-empty it set the color scale limits of the
        similarity matrix image
    :param ylim: Limits for the stretch axis. Defaults to None
    :type ylim: Tuple[float, float], optional
    :param return_ax: Return plt.figure and list of axes. Defaults to False.
        This overwrites any choice to save the figure.
    :type return_ax: bool, optional
    :returns: If `return_ax` is set to True it returns fig and axes.
    """
    set_mpl_params()

    if sim_mat_Clim and len(sim_mat_Clim) != 2:
        raise ValueError('Sim_mat_Clim has to be a list or Tuple of length 2.')

    if figure_file_name:
        os.makedirs(save_dir, exist_ok=True)

    # Create a unique filename if TraitsUI-default is given
    if figure_file_name == 'plot_default':
        fname = figure_file_name + '_change.png'
        exist = os.path.isfile(os.path.join(save_dir, fname))
        i = 0
        while exist:
            fname = "%s_%i" % (figure_file_name, i)
            exist = os.path.isfile(os.path.join(save_dir,
                                                fname + '_change.png'))
            i += 1
        figure_file_name = fname

    # Extract the data from the dictionary

    value_type = dv.value_type
    method = dv.method
    corr = dv.corr
    dt = dv.value
    sim_mat = dv.sim_mat
    stretch_vect = dv.second_axis
    stats = dv.stats
    plot_sim_mat = sim_mat.size > 0

    rtime = np.array(
        [utcdt.datetime for utcdt in stats['corr_start']],
        dtype=np.datetime64)

    # normalize simmat if requested and if dv contains simmat
    if normalize_simmat and plot_sim_mat:
        sim_mat = sim_mat/np.tile(
            np.max(sim_mat, axis=1), (sim_mat.shape[1], 1)).T

    stretching_amount = np.max(stretch_vect)

    # Adapt plot details in agreement with the type of dictionary that
    # has been passed

    if (value_type == 'stretch') and (method == 'single_ref'):

        tit = "Single reference dv/v"
        # Find order of magnitude of max strech
        oom = -int(np.floor(np.log10(stretch_vect.max()/5)))
        dv_tick_delta = round(stretch_vect.max()/5, oom)
        dv_y_label = "dv/v"
        # plotting velocity requires to flip the stretching axis
    elif (value_type == 'stretch') and (method == 'multi_ref'):

        # Find order of magnitude of max strech
        oom = -int(np.floor(np.log10(stretch_vect.max()/5)))
        tit = "Multi reference dv/v"
        dv_tick_delta = round(stretch_vect.max()/5, oom)
        dv_y_label = "dv/v"
        # plotting velocity requires to flip the stretching axis
    elif (value_type == 'shift') and (method == 'time_shift'):

        tit = "Time shift"
        dv_tick_delta = 5
        dv_y_label = "Time Shift (sample)"
    elif (value_type == 'shift') and (method == 'absolute_shift'):
        tit = "Time shift"
        dv_tick_delta = (
            np.max(dv.second_axis)
            - np.min(dv.second_axis))/5
        dv_y_label = "Time Shift [s]"
    else:
        raise ValueError(f"Unknown dv type, {value_type}!")

    f = plt.figure(figsize=figsize, dpi=dpi)

    if dv.n_stat is not None and plot_scatter:
        gs = mpl.gridspec.GridSpec(4, 1, height_ratios=[12, 4, 4, 1])
    else:
        gs = mpl.gridspec.GridSpec(3, 1, height_ratios=[3, 1, 1])

    ax1 = f.add_subplot(gs[0])
    if plot_sim_mat:
        imh = plt.imshow(
            np.flipud(sim_mat.T).astype(float), interpolation='none',
            aspect='auto')

    # plotting value is way easier now
    plt.plot(-dv.value, 'b.')

    # Set extent so we can treat the axes properly (mainly y)
    if plot_sim_mat:
        imh.set_extent((
            0, sim_mat.shape[0], stretch_vect[-1], stretch_vect[0]))

    ###
    if xlim:
        xlim = (np.datetime64(xlim[0]), np.datetime64(xlim[1]))
        xl0 = np.argmin(abs(rtime-xlim[0]))
        xl1 = np.argmin(abs(rtime-xlim[1]))
        ax1.set_xlim(xl0, xl1+1)
        plt.xlim(xl0, xl1+1)
    else:
        ax1.set_xlim(0, len(dv.value))
        plt.xlim(0, len(dv.value))

    if value_type == 'stretch':
        ax1.invert_yaxis()
    if ylim:
        plt.ylim(ylim)
    # ###
    if sim_mat_Clim and plot_sim_mat:
        imh.set_clim(sim_mat_Clim[0], sim_mat_Clim[1])

    plt.gca().get_xaxis().set_visible(False)

    comb_mseedid = '%s.%s.%s' % (
        stats['network'], stats['station'],  # stats['location'],
        stats['channel'])
    if title:
        tit = title
    else:
        tit = "%s estimate (%s)" % (tit, comb_mseedid)

    ax1.set_title(tit)
    ax1.yaxis.set_ticks_position('right')
    ax1.yaxis.set_label_position('left')
    ax1.set_xticklabels([])
    ax1.set_ylabel(dv_y_label)

    ax2 = f.add_subplot(gs[1])
    if plot_scatter:
        # reshape so we can plot a histogram
        histt = np.array([t.timestamp for t in stats['corr_start']])
        histt = np.tile(histt, dv.stretches.shape[0])
        histcorrs = np.reshape(dv.corrs, -1)
        histstretches = np.reshape(-dv.stretches, -1)
        n_bins = np.flip(np.array(dv.sim_mat.shape))//10
        # remove nans
        nanmask = ~np.isnan(histcorrs)
        histt = histt[nanmask]
        histcorrs = histcorrs[nanmask]
        histstretches = histstretches[nanmask]
        # create histograms
        H_c, xedges_c, yedges_c = np.histogram2d(histt, histcorrs, bins=n_bins)
        H_v, xedges_v, yedges_v = np.histogram2d(
            histt, histstretches, bins=n_bins)
        xedges_c = [UTCDateTime(xe).datetime for xe in xedges_c]
        xedges_v = [UTCDateTime(xe).datetime for xe in xedges_v]
        plt.pcolor(xedges_v, yedges_v, H_v.T, cmap='binary')
    plt.plot(rtime, -dt, '.', markersize=3)
    if xlim:
        plt.xlim(xlim[0], xlim[1])
    else:
        plt.xlim([rtime[0], rtime[-1]])
    if ylim:
        plt.ylim(ylim)
    else:
        plt.ylim((-stretching_amount, stretching_amount))
    if mark_time and not (
            np.all(rtime < mark_time) and np.all(rtime > mark_time)):
        plt.axvline(mark_time, lw=1, color='r')
    ax2.yaxis.set_ticks_position('left')
    ax2.yaxis.set_label_position('right')
    ax2.yaxis.label.set_rotation(270)
    ax2.yaxis.set_label_coords(1.03, 0.5)
    ax2.set_ylabel(dv_y_label)
    ax2.yaxis.set_major_locator(plt.MultipleLocator(dv_tick_delta))
    ax2.yaxis.grid(True, 'major', linewidth=1)
    ax2.xaxis.grid(True, 'major', linewidth=1)
    ax2.set_xticklabels([])

    ax3 = f.add_subplot(gs[2])
    if plot_scatter:
        plt.pcolor(xedges_c, yedges_c, H_c.T, cmap='binary')
    plt.plot(rtime, corr, '.', markersize=3)

    if xlim:
        plt.xlim(xlim[0], xlim[1])
    else:
        plt.xlim([rtime[0], rtime[-1]])
    ax3.yaxis.set_ticks_position('right')
    ax3.set_ylabel("Correlation")
    plt.ylim((0, 1))
    if mark_time and not (
            np.all(rtime < mark_time) and np.all(rtime > mark_time)):
        plt.axvline(mark_time, lw=1, color='r')
    ax3.yaxis.grid(True, 'major', linewidth=1)
    ax3.xaxis.grid(True, 'major', linewidth=1)
    ax3.yaxis.set_major_locator(plt.MultipleLocator(0.2))
    # Plot number of stations
    if dv.n_stat is not None and plot_scatter:
        ax4 = f.add_subplot(gs[3])
        plt.fill_between(
            rtime, 0, dv.n_stat, interpolate=False)
        plt.setp(ax4.get_xticklabels(), rotation=45, ha='right')
        ax3.set_xticklabels([])
        ax4.yaxis.set_label_position('right')
        ax4.set_ylabel("N")
        plt.ylim(0, int(dv.n_stat.max()*1.1))
        if xlim:
            plt.xlim(xlim[0], xlim[1])
        else:
            plt.xlim([rtime[0], rtime[-1]])
    else:
        ax4 = None
        plt.setp(ax3.get_xticklabels(), rotation=45, ha='right')

    plt.subplots_adjust(hspace=0, wspace=0)
    if return_ax:
        return f, [ax1, ax2, ax3, ax4]
    if figure_file_name is None:
        plt.show()
    else:
        print('saving to %s' % figure_file_name)
        if figure_file_name.split('.')[-1].lower() not in [
                'png', 'svg', 'pdf', 'jpg']:
            figure_file_name += '.png'
        f.savefig(os.path.join(save_dir, figure_file_name),
                  dpi=dpi)


def plot_dv(style: str, *args, **kwargs):
    if style == 'technical':
        return plot_technical_dv(*args, **kwargs)
    elif style == 'publication':
        return plot_fancy_dv(*args, **kwargs)
    else:
        raise ValueError(f'Unknown style: {style}')
