#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# QTPyLib: Quantitative Trading Python Library
# https://github.com/ranaroussi/qtpylib
#
# Copyright 2016 Ran Aroussi
#
# Licensed under the GNU Lesser General Public License, v3.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.gnu.org/licenses/lgpl-3.0.en.html
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import datetime
import threading

import numpy as np
import pandas as pd
import time
import os
from stat import S_IWRITE
from math import ceil

from dateutil.relativedelta import relativedelta, FR
from dateutil.parser import parse as parse_date
from pytz import timezone

# for re-export
from ezibpy.utils import (
    createLogger, order_to_dict, contract_to_dict
)

# =============================================
def chmod(f):
    """ change mod to writeable """
    try: os.chmod(f, S_IWRITE) # windows (cover all)
    except: pass
    try: os.chmod(f, 0o777) # *nix
    except: pass

# =============================================
def as_dict(df, ix=':'):
    """ converts df to dict and adds a datetime field if df is datetime """
    if isinstance(df.index, pd.DatetimeIndex):
        df['datetime'] = df.index
    return df.to_dict(orient='records')[ix]

# =============================================
def is_number(string):
    """ checks if a string is a number (int/float) """
    string = str(string)
    if string.isnumeric():
        return True
    try:
        float(string)
        return True
    except ValueError:
        return False

# =============================================
def ib_duration_str(start_date=None):
    """
    Get a datetime object or a epoch timestamp and return
    an IB-compatible durationStr for reqHistoricalData()
    """
    now = datetime.datetime.utcnow()

    if is_number(start_date):
        diff = now - datetime.datetime.fromtimestamp(float(start_date))
    elif isinstance(start_date, str):
        diff = now - parse_date(start_date)
    elif isinstance(start_date, datetime.datetime):
        diff = now - start_date
    else:
        return None

    # get diff
    second_diff = diff.seconds
    day_diff = diff.days

    # small diff?
    if day_diff < 0 or second_diff < 60:
        return None

    # return str(second_diff)+ " S"
    if day_diff == 0:
        return str(second_diff+3600)+ " S"
    if 31 > day_diff > 0:
        return str(day_diff) + " D"
    if 365 > day_diff > 31:
        return str(ceil(day_diff / 30)) + " M"

    return str(ceil(day_diff / 365)) + " Y"

# =============================================
def datetime64_to_datetime(dt):
    """ convert numpy's datetime64 to datetime """
    dt64 = np.datetime64(dt)
    ts = (dt64 - np.datetime64('1970-01-01T00:00:00')) / np.timedelta64(1, 's')
    return datetime.datetime.utcfromtimestamp(ts)

# =============================================
# utility to get the machine's timeozone
# =============================================
def get_timezone():
    try:
        offsetHour = -(datetime.datetime.now()-datetime.datetime.utcnow()).seconds
    except:
        if time.daylight:
            offsetHour = time.altzone
        else:
            offsetHour = time.timezone

    return 'Etc/GMT%+d' % round(offsetHour / 3600)

def datetime_to_timezone(date, tz="UTC", from_tz=None):
    if not date.tzinfo:
        date = date.replace(tzinfo=timezone(get_timezone()))
    return date.astimezone(timezone(tz))


def convert_timezone(date_str, tz_from, tz_to="UTC", fmt=None):
    # get timezone as tz_offset
    tz_offset = datetime_to_timezone(datetime.datetime.now(), tz=tz_from).strftime('%z')
    tz_offset = tz_offset[:3]+':'+tz_offset[3:]

    date = parse_date(str(date_str)+tz_offset)
    if tz_from != tz_to:
        date = datetime_to_timezone(date, tz_to)

    if isinstance(fmt, str):
        return date.strftime(fmt)
    return date

# =============================================
# utility to change the timeozone to specified one
# =============================================
def set_timezone(data, tz=None, from_local=False):
    # pandas object?
    if isinstance(data, pd.DataFrame) | isinstance(data, pd.Series):
        try:
            try:
                data.index = data.index.tz_convert(tz)
            except:
                if from_local:
                    data.index = data.index.tz_localize(get_timezone()).tz_convert(tz)
                else:
                    data.index = data.index.tz_localize('UTC').tz_convert(tz)
        except: pass

    # not pandas...
    else:
        if isinstance(data, str):
            data = parse_date(data)
        try:
            try:
                data = data.astimezone(tz)
            except:
                data = timezone('UTC').localize(data).astimezone(timezone(tz))
        except: pass

    return data

# =============================================
# set timezone for pandas
# =============================================
def fix_timezone(df, freq, tz=None):
    index_name = df.index.name

    # fix timezone
    if isinstance(df.index[0], str):
        # timezone df exists
        if ("-" in df.index[0][-6:]) | ("+" in df.index[0][-6:]):
            df.index = pd.to_datetime(df.index, utc=False)
            df.index = df.index.tz_localize('UTC').tz_convert(tz)

        # no timezone df - do some resampling
        else:
            # original range
            start_range = df.index[0]
            end_range   = df.index[-1]

            # resample df
            df.index = pd.to_datetime(df.index, utc=True)
            df = resample(df, freq=freq, ffill=False, dropna=False)

            # create date range
            new_freq = ''.join(i for i in freq if not i.isdigit())
            rng = pd.date_range(start=start_range, end=end_range, tz=tz, freq=new_freq)

            # assign date range to df and drop empty rows
            df.index = rng
            df.dropna(inplace=True)

    # finalize timezone (also for timezone-aware df)
    df = set_timezone(df, tz=tz)

    df.index.name = index_name
    return df

# ===========================================
# resample baed on time / tick count
# ===========================================
def resample(data, resolution="1T", tz=None, ffill=True, dropna=False):

    def resample_ticks(data, freq=1000, by='last'):
        """
        function that re-samples tick data into an N-tick or N-volume OHLC format

        df = pandas pd.dataframe of raw tick data
        freq = resoltuin grouping
        by = the column name to resample by
        """

        data.fillna(value=np.nan, inplace=True)

        # get only ticks and fill missing data
        try:
            df = data[['last', 'lastsize', 'opt_underlying', 'opt_price',
                'opt_dividend', 'opt_volume', 'opt_iv', 'opt_oi',
                'opt_delta', 'opt_gamma', 'opt_theta', 'opt_vega']].copy()
            price_col = 'last'
            size_col  = 'lastsize'
        except:
            df = data[['close', 'volume', 'opt_underlying', 'opt_price',
                'opt_dividend', 'opt_volume', 'opt_iv', 'opt_oi',
                'opt_delta', 'opt_gamma', 'opt_theta', 'opt_vega']].copy()
            price_col = 'close'
            size_col  = 'volume'

        # add group indicator evey N df
        if by == 'size' or by == 'lastsize' or by == 'volume':
            df['cumvol'] = df[size_col].cumsum()
            df['mark'] = round(round(round(df['cumvol'] / .1)*.1, 2)/freq) * freq
            df['diff'] = df['mark'].diff().fillna(0).astype(int)
            df['grp'] = np.where(df['diff']>=freq-1, (df['mark']/freq), np.nan)
        else:
            df['grp'] = [np.nan if i%freq else i for i in range(len(df[price_col]))]

        df.loc[:1, 'grp'] = 0

        df.fillna(method='ffill', inplace=True)

        # print(df[['lastsize', 'cumvol', 'mark', 'diff', 'grp']].tail(1))

        # place timestamp index in T colums
        # (to be used as future df index)
        df['T'] = df.index

        # make group the index
        df = df.set_index('grp')

        # grop df
        groupped = df.groupby(df.index, sort=False)

        # build ohlc(v) pd.dataframe from new grp column
        newdf = pd.DataFrame({
            'open':   groupped[price_col].first(),
            'high':   groupped[price_col].max(),
            'low':    groupped[price_col].min(),
            'close':  groupped[price_col].last(),
            'volume': groupped[size_col].sum(),

            'opt_price':      groupped['opt_price'].last(),
            'opt_underlying': groupped['opt_underlying'].last(),
            'opt_dividend':   groupped['opt_dividend'].last(),
            'opt_volume':     groupped['opt_volume'].last(),
            'opt_iv':         groupped['opt_iv'].last(),
            'opt_oi':         groupped['opt_oi'].last(),
            'opt_delta':      groupped['opt_delta'].last(),
            'opt_gamma':      groupped['opt_gamma'].last(),
            'opt_theta':      groupped['opt_theta'].last(),
            'opt_vega':       groupped['opt_vega'].last()
        })

        # set index to timestamp
        newdf['datetime'] = groupped.T.head(1)
        newdf.set_index(['datetime'], inplace=True)

        return newdf


    if len(data) > 0:

        # resample
        periods = int("".join([s for s in resolution if s.isdigit()]))
        meta_data = data.groupby(["symbol"])[['symbol', 'symbol_group', 'asset_class']].last()
        combined = []

        if ("K" in resolution):
            if (periods > 1):
                for sym in meta_data.index.values:
                    # symdata = resample_ticks(data[data['symbol']==sym], periods, price_col='last', volume_col='lastsize')
                    symdata = resample_ticks(data[data['symbol']==sym].copy(), freq=periods, by='last')
                    symdata['symbol'] = sym
                    symdata['symbol_group'] = meta_data[meta_data.index==sym]['symbol_group'].values[0]
                    symdata['asset_class'] = meta_data[meta_data.index==sym]['asset_class'].values[0]

                    # cleanup
                    symdata.dropna(inplace=True, subset=['open', 'high', 'low', 'close', 'volume'])
                    if sym[-3:] in ("OPT", "FOP"):
                        symdata.dropna(inplace=True)

                    combined.append(symdata)

                data = pd.concat(combined)

        elif ("V" in resolution):
            if (periods > 1):
                for sym in meta_data.index.values:
                    symdata = resample_ticks(data[data['symbol']==sym].copy(), freq=periods, by='lastsize')
                    # print(symdata)
                    symdata['symbol'] = sym
                    symdata['symbol_group'] = meta_data[meta_data.index==sym]['symbol_group'].values[0]
                    symdata['asset_class'] = meta_data[meta_data.index==sym]['asset_class'].values[0]

                    # cleanup
                    symdata.dropna(inplace=True, subset=['open', 'high', 'low', 'close', 'volume'])
                    if sym[-3:] in ("OPT", "FOP"):
                        symdata.dropna(inplace=True)

                    combined.append(symdata)

                data = pd.concat(combined)

        # continue...
        else:
            ticks_ohlc_dict = {
                'lastsize':       'sum',
                'opt_price':      'last',
                'opt_underlying': 'last',
                'opt_dividend':   'last',
                'opt_volume':     'last',
                'opt_iv':         'last',
                'opt_oi':         'last',
                'opt_delta':      'last',
                'opt_gamma':      'last',
                'opt_theta':      'last',
                'opt_vega':       'last'
            }
            bars_ohlc_dict = {
                'open':           'first',
                'high':           'max',
                'low':            'min',
                'close':          'last',
                'volume':         'sum',
                'opt_price':      'last',
                'opt_underlying': 'last',
                'opt_dividend':   'last',
                'opt_volume':     'last',
                'opt_iv':         'last',
                'opt_oi':         'last',
                'opt_delta':      'last',
                'opt_gamma':      'last',
                'opt_theta':      'last',
                'opt_vega':       'last'
            }

            for sym in meta_data.index.values:
                if ("S" in resolution):
                    ohlc = data[data['symbol']==sym]['last'].resample(resolution).ohlc()
                    symdata = data[data['symbol']==sym].resample(resolution).apply(ticks_ohlc_dict).fillna(value=np.nan)
                    symdata.rename(columns={'lastsize': 'volume'}, inplace=True)
                    symdata['open']  = ohlc['open']
                    symdata['high']  = ohlc['high']
                    symdata['low']   = ohlc['low']
                    symdata['close'] = ohlc['close']
                else:
                    original_length = len(data[data['symbol']==sym])
                    symdata = data[data['symbol']==sym].resample(resolution).apply(bars_ohlc_dict).fillna(value=np.nan)

                    # deal with new rows caused by resample
                    if len(symdata) > original_length:
                        # volume is 0 on rows created using resample
                        symdata['volume'].fillna(0, inplace=True)
                        symdata.ffill(inplace=True)

                        # no fill / return original index
                        if ffill:
                            symdata['open']  = np.where(symdata['volume']<=0, symdata['close'], symdata['open'])
                            symdata['high']  = np.where(symdata['volume']<=0, symdata['close'], symdata['high'])
                            symdata['low']   = np.where(symdata['volume']<=0, symdata['close'], symdata['low'])
                        else:
                            symdata['open']  = np.where(symdata['volume']<=0, np.nan, symdata['open'])
                            symdata['high']  = np.where(symdata['volume']<=0, np.nan, symdata['high'])
                            symdata['low']   = np.where(symdata['volume']<=0, np.nan, symdata['low'])
                            symdata['close'] = np.where(symdata['volume']<=0, np.nan, symdata['close'])

                    # drop NANs
                    if dropna:
                        symdata.dropna(inplace=True)

                symdata['symbol'] = sym
                symdata['symbol_group'] = meta_data[meta_data.index==sym]['symbol_group'].values[0]
                symdata['asset_class'] = meta_data[meta_data.index==sym]['asset_class'].values[0]

                # cleanup
                symdata.dropna(inplace=True, subset=['open', 'high', 'low', 'close', 'volume'])
                if sym[-3:] in ("OPT", "FOP"):
                    symdata.dropna(inplace=True)

                combined.append(symdata)

            data = pd.concat(combined)
            data['volume'] = data['volume'].astype(int)

    # figure out timezone
    if tz is None:
        try:
            tz = str(data.index.tz)
        except:
            tz = None

    if tz is not None:
        try:
            data.index = data.index.tz_convert(tz)
        except:
            data.index = data.index.tz_localize('UTC').tz_convert(tz)

    return data

# -------------------------------------------
class make_object:
    def __init__(self, **entries):
        self.__dict__.update(entries)

# -------------------------------------------
def round_to_fraction(val, res, decimals=None):
    """ round to closest resolution """
    if decimals is None and "." in str(res):
        decimals = len(str(res).split('.')[1])

    return round(round(val / res)*res, decimals)

# -------------------------------------------
def backdate(res, date=None, as_datetime=False, fmt='%Y-%m-%d', tz="UTC"):
    if date is None:
        date = datetime.datetime.now()
    else:
        try: date = parse_date(date)
        except: pass

    new_date = date

    periods = int("".join([s for s in res if s.isdigit()]))

    if periods > 0:

        if "K" in res:
            new_date = date - datetime.timedelta(microseconds=periods)
        elif "S" in res:
            new_date = date - datetime.timedelta(seconds=periods)
        elif "T" in res:
            new_date = date - datetime.timedelta(minutes=periods)
        elif "H" in res or "V" in res:
            new_date = date - datetime.timedelta(hours=periods)
        elif "W" in res:
            new_date = date - datetime.timedelta(weeks=periods)
        else: # days
            new_date = date - datetime.timedelta(days=periods)

        # not a week day:
        while new_date.weekday() > 4: # Mon-Fri are 0-4
            new_date = backdate(res="1D", date=new_date, as_datetime=True)

    if as_datetime:
        return new_date
    else:
        return new_date.strftime(fmt)

# -------------------------------------------
def previous_weekday(day=None, as_datetime=False):
    if day is None:
        day = datetime.datetime.now()
    else:
        day = datetime.datetime.strptime(day, '%Y-%m-%d')

    day -= datetime.timedelta(days=1)
    while day.weekday() > 4: # Mon-Fri are 0-4
        day -= datetime.timedelta(days=1)

    if as_datetime:
        return day
    return day.strftime("%Y-%m-%d")

# -------------------------------------------
def is_third_friday(day=None):
    if day is None: day = datetime.datetime.now()
    defacto_friday = (day.weekday() == 4) or (day.weekday() == 3 and day.hour() >= 17)
    return defacto_friday and 14 < day.day < 22

# -------------------------------------------
def after_third_friday(day=None):
    if day is None: day = datetime.datetime.now()
    now = day.replace(day=1, hour=16, minute=0, second=0, microsecond=0)
    now += relativedelta(weeks=2, weekday=FR)
    return day > now


# ===========================================
# store event in a temp data store
# ===========================================
class DataStore():
    def __init__(self, output_file=None):
        self.auto = None
        self.recorded = None
        self.output_file = output_file

    def record(self, timestamp, *args, **kwargs):
        """ add custom data to data store """
        if self.output_file is None:
            return

        data = {}

        # append all data
        if len(args) == 1:
            if isinstance(args[0], dict):
                data.update(dict(args[0]))
            elif isinstance(args[0], pd.DataFrame):
                data.update(args[0][-1:].to_dict(orient='records')[0])

        # add kwargs
        if len(kwargs) > 0:
            data.update(dict(kwargs))

        # set the datetime
        data['datetime'] = timestamp

        # take datetime from index
        if self.recorded is not None:
            self.recorded['datetime'] = self.recorded.index

        row = pd.DataFrame(data=data, index=[timestamp])
        if self.recorded is None:
            self.recorded = row
        else:
            self.recorded.merge(row)
            self.recorded = pd.concat([self.recorded, row])

        # merge rows (play nice with multi-symbol portfolios)
        meta_data = self.recorded.groupby(["symbol"])[['symbol', 'symbol_group', 'asset_class']].last()
        combined = []

        for sym in meta_data.index.values:
            df = self.recorded[self.recorded['symbol']==sym].copy()
            symdata = df.groupby(df.index).sum()
            symdata.index.rename('datetime', inplace=True)

            symdata['symbol'] = sym
            symdata['symbol_group'] = df['symbol_group'].values[0]
            symdata['asset_class'] = df['asset_class'].values[0]

            combined.append(symdata)

        self.recorded = pd.concat(combined)

        # cleanup: remove non-option data if not working with options
        opt_cols = df.columns[df.columns.str.startswith('opt_')].tolist()
        if len(opt_cols) == len(df[ opt_cols ].isnull().all()):
            self.recorded.drop(opt_cols, axis=1, inplace=True)

        # cleanup: positions
        if "position" in self.recorded.columns:
            self.recorded['position'].ffill(inplace=True)
        else:
            self.recorded.loc[:, 'position'] = 0

        self.recorded['position'] = self.recorded['position'].astype(int)

        # cleanup: symbol names
        data = self.recorded.copy()
        for asset_class in data['asset_class'].unique().tolist():
            data['symbol'] = data['symbol'].str.replace("_"+str(asset_class), "")

        # save
        if ".csv" in self.output_file:
            data.to_csv(self.output_file)
        elif ".h5" in self.output_file:
            data.to_hdf(self.output_file, 0)
        elif (".pickle" in self.output_file) | (".pkl" in self.output_file):
            data.to_pickle(self.output_file)

        chmod(self.output_file)

# =============================================
class RecurringTask(threading.Thread):
    """Calls a function at a sepecified interval."""
    def __init__(self, func, interval_sec, init_sec=0, *args, **kwargs):
        """Call `func` every `interval_sec` seconds.

        Starts the timer.

        Accounts for the runtime of `func` to make intervals as close to `interval_sec` as possible.
        args and kwargs are passed to Thread().

        :Parameters:
            func : object
                Function to invoke every N seconds
            interval_sec : int
                Call func every this many seconds
            init_sec : int
                Wait this many seconds initially before the first call
            *args : mixed
                parameters sent to parent Thread class
            **kwargs : mixed
                parameters sent to parent Thread class
        """

        # threading.Thread.__init__(self, *args, **kwargs) # For some reason super() doesn't work
        super().__init__(*args, **kwargs) # Works!
        self._func        = func
        self.interval_sec = interval_sec
        self.init_sec     = init_sec
        self._running     = True
        self._functime    = None # Time the next call should be made

        self.start()

    def __repr__(self):
        return 'RecurringTask({}, {}, {})'.format(self._func, self.interval_sec, self.init_sec)

    def run(self):
        """Start the recurring task."""
        if self.init_sec:
            time.sleep(self.init_sec)
        self._functime = time.time()
        while self._running:
            start = time.time()
            self._func()
            self._functime += self.interval_sec
            if self._functime - start > 0:
                time.sleep(self._functime - start)

    def stop(self):
        """Stop the recurring task."""
        self._running = False
