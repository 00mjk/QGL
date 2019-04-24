'''
Channels is where we store information for mapping virtual (qubit) channel to
real channels.

Split from Channels.py on Jan 14, 2016.
Moved to SQLAlchemy ORM from atom 2018

Original Author: Colm Ryan
Modified By: Graham Rowlands

Copyright 2016-2018 Raytheon BBN Technologies

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

   http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.

Include modification to yaml loader (MIT License) from
https://gist.github.com/joshbode/569627ced3076931b02f

Scientific notation fix for yaml from
https://stackoverflow.com/questions/30458977/yaml-loads-5e-6-as-string-and-not-a-number
'''

import sys
import os
import re
import datetime
import traceback
import datetime
import importlib
import inspect
from functools import wraps
import itertools
import numpy as np
import networkx as nx
import logging

import bbndb

from . import config
from . import Channels
from . import PulseShapes
from .PulsePrimitives import clear_pulse_cache

from IPython.display import HTML, display

channelLib = None

logger = logging.getLogger("QGL")

def check_session_dirty(f):
    """Since we can't mix db objects from separate sessions, re-fetch entities by their unique IDs"""
    @wraps(f)
    def wrapper(cls, *args, **kwargs):
        if (len(cls.session.dirty | cls.session.new)) == 0:
            if 'force' in kwargs:
                kwargs.pop('force')
            return f(cls, *args, **kwargs)
        elif 'force' in kwargs and kwargs['force']:
            kwargs.pop('force')
            return f(cls, *args, **kwargs)
        else:
            raise Exception("Uncommitted transactions for working database. Either use force=True or commit/revert your changes.")
    return wrapper

def check_for_duplicates(f):
    """Since we can't mix db objects from separate sessions, re-fetch entities by their unique IDs"""
    @wraps(f)
    def wrapper(cls, label, *args, **kwargs):
        if label in cls.channelDict:
            logger.warning(f"A database item with the name {label} already exists. Updating parameters of this existing item instead.")
            cls.channelDict[label].__dict__.update(kwargs)
            return cls.channelDict[label]  #should check for difference in args
        else:
            return f(cls, label, *args, **kwargs)
    return wrapper

class ChannelLibrary(object):

    def __init__(self, db_resource_name=":memory:", db_provider="sqlite"):
        """Create the channel library."""

        global channelLib

        bbndb.initialize_db(f'{db_provider}:///{db_resource_name}')
        self.session = bbndb.get_cl_session()
        self.connectivityG = nx.DiGraph()
        self.db_provider = db_provider
        self.db_resource_name = db_resource_name

        # Check to see whether there is already a temp database
        working_dbs = self.query(Channels.ChannelDatabase, label="working").all()
        if len(working_dbs) > 1:
            raise Exception("More than one working database exists!")
        elif len(working_dbs) == 1:
            self.channelDatabase = working_dbs[0]
        elif len(working_dbs) == 0:
            self.channelDatabase = Channels.ChannelDatabase(label="working", time=datetime.datetime.now())
            self.add_and_update_dict(self.channelDatabase)
            self.session.commit()

        self.update_channelDict()

        # Update the global reference
        channelLib = self

    def query(self, obj_type, **kwargs):
        return self.session.query(obj_type).filter_by(**kwargs)

    def get_current_channels(self):
        return (self.channelDatabase.channels +
               self.channelDatabase.generators +
               self.channelDatabase.transmitters +
               self.channelDatabase.receivers +
               self.channelDatabase.transceivers +
               self.channelDatabase.instruments +
               self.channelDatabase.processors +
               self.channelDatabase.attenuators +
               self.channelDatabase.DCSources +
               self.channelDatabase.spectrum_analyzers)

    def update_channelDict(self):
        self.channelDict = {c.label: c for c in self.get_current_channels()}
        self.build_connectivity_graph()

    def ls(self):
        cdb = Channels.ChannelDatabase
        q = self.session.query(cdb.label, cdb.time, cdb.id).\
            order_by(Channels.ChannelDatabase.id, Channels.ChannelDatabase.label).all()
        table_code = ""
        for i, (label, time, id) in enumerate(q):
            y, d, t = map(time.strftime, ["%Y", "%b. %d", "%I:%M:%S %p"])
            # t = time.strftime("(%Y) %b. %d @ %I:%M:%S %p")
            table_code += f"<tr><td>{id}</td><td>{y}</td><td>{d}</td><td>{t}</td><td>{label}</td></tr>"
        display(HTML(f"<table><tr><th>id</th><th>Year</th><th>Date</th><th>Time</th><th>Name</th></tr><tr>{table_code}</tr></table>"))

    def ent_by_type(self, obj_type, show=False):
        q = self.session.query(obj_type).filter(obj_type.channel_db.has(label="working")).order_by(obj_type.label).all()
        if show:
            for i, el in enumerate(q):
                print(f"[{i}] -> {el.label}")
        else:
            return q

    def receivers(self):
        return self.ent_by_type(Channels.Receiver)

    def transmitters(self):
        return self.ent_by_type(Channels.Transmitter)

    def transceivers(self):
        return self.ent_by_type(Channels.Transceiver)

    def qubits(self):
        return self.ent_by_type(Channels.Qubit)

    def meas(self):
        return self.ent_by_type(Channels.Measurement)

    def markers(self):
       return self.ent_by_type(Channels.LogicalMarkerChannel)

    @check_session_dirty
    def load(self, name, index=1):
        """Load the latest instance for a particular name. Specifying index = 2 will select the second most recent instance """
        cdb = Channels.ChannelDatabase
        items = self.session.query(cdb).filter(cdb.label==name).order_by(cdb.time.desc()).all()
        self.load_obj(items[-index])

    @check_session_dirty
    def load_by_id(self, id_num):
        item = self.session.query(Channels.ChannelDatabase).filter_by(id=id_num).first()
        self.load_obj(item)

    def clear(self, channel_db=None, create_new=True):
        # If no database is specified, clear self.database
        channel_db = channel_db if channel_db else self.channelDatabase

        self.session.delete(channel_db)
        self.session.commit()

        if create_new:
            self.channelDatabase = Channels.ChannelDatabase(label="working", time=datetime.datetime.now())
            self.add_and_update_dict(self.channelDatabase)
            self.session.commit()
        channelLib = self

    def rm(self, library_name, keep_id=-1):
        """Remove the channel library named `library_name`. If no `keep_version` is specified then
        all versions are removed. Otherwise """
        cdb = Channels.ChannelDatabase
        items = self.session.query(cdb).filter(cdb.label==library_name and cdb.id!=keep_id).all()
        for item in items:
            self.session.delete(item)

    def rm_by_id(self, id):
        """Remove the channel library with id `id`"""
        item = self.session.query(Channels.ChannelDatabase).filter_by(id=id_num).first()
        self.session.delete(item)

    def load_obj(self, obj):
        self.clear(create_new=False)
        self.channelDatabase = bbndb.deepcopy_sqla_object(obj, self.session)
        self.channelDatabase.label = "working"
        self.session.commit()
        self.update_channelDict()

    def commit(self):
        self.session.commit()
        self.update_channelDict()

    def revert(self):
        self.session.rollback()

    @check_session_dirty
    def save_as(self, name):
        if name == "working":
            raise ValueError("Cannot save as `working` since that is the default working environment name...")
        self.commit()
        new_channelDatabase = bbndb.deepcopy_sqla_object(self.channelDatabase, self.session)
        new_channelDatabase.label = name
        new_channelDatabase.time = datetime.datetime.now()
        self.commit()

    def add_and_update_dict(self, el):
        if isinstance(el, list):
            self.session.add_all(el)
        else:
            self.session.add(el)
        self.update_channelDict()

    #Dictionary methods
    def __getitem__(self, key):
        return self.channelDict[key]

    def __setitem__(self, key, value):
        self.channelDict[key] = value

    def __delitem__(self, key):
        del self.channelDict[key]

    def __contains__(self, key):
        return key in self.channelDict

    def keys(self):
        return self.channelDict.keys()

    def values(self):
        return self.channelDict.values()

    def build_connectivity_graph(self):
        # build connectivity graph
        for chan in self.session.query(Channels.Qubit).filter(Channels.Qubit not in self.connectivityG).all():
            self.connectivityG.add_node(chan)
        for chan in self.session.query(Channels.Edge): #select(e for e in Channels.Edge):
            self.connectivityG.add_edge(chan.source, chan.target)
            self.connectivityG[chan.source][chan.target]['channel'] = chan

    @check_for_duplicates
    def new_APS2(self, label, address, **kwargs):
        chan1  = Channels.PhysicalQuadratureChannel(label=f"{label}-1", channel=0, instrument=label, translator="APS2Pattern", channel_db=self.channelDatabase)
        m1     = Channels.PhysicalMarkerChannel(label=f"{label}-m1", channel=0, instrument=label, translator="APS2Pattern", channel_db=self.channelDatabase)
        m2     = Channels.PhysicalMarkerChannel(label=f"{label}-m2", channel=1, instrument=label, translator="APS2Pattern", channel_db=self.channelDatabase)
        m3     = Channels.PhysicalMarkerChannel(label=f"{label}-m3", channel=2, instrument=label, translator="APS2Pattern", channel_db=self.channelDatabase)
        m4     = Channels.PhysicalMarkerChannel(label=f"{label}-m4", channel=3, instrument=label, translator="APS2Pattern", channel_db=self.channelDatabase)

        this_transmitter = Channels.Transmitter(label=label, model="APS2", address=address, channels=[chan1, m1, m2, m3, m4], channel_db=self.channelDatabase, **kwargs)
        this_transmitter.trigger_source = "external"
        this_transmitter.address        = address

        self.add_and_update_dict(this_transmitter)
        return this_transmitter

    @check_for_duplicates
    def new_APS(self, label, address, **kwargs):
        chan1  = Channels.PhysicalQuadratureChannel(label=f"{label}-12", channel = 0, instrument=label, translator="APSPattern", channel_db=self.channelDatabase)
        chan2  = Channels.PhysicalQuadratureChannel(label=f"{label}-34", channel = 1, instrument=label, translator="APSPattern", channel_db=self.channelDatabase)
        m1     = Channels.PhysicalMarkerChannel(label=f"{label}-1m1", channel=0, instrument=label, translator="APSPattern", channel_db=self.channelDatabase)
        m2     = Channels.PhysicalMarkerChannel(label=f"{label}-2m1", channel=1, instrument=label, translator="APSPattern", channel_db=self.channelDatabase)
        m3     = Channels.PhysicalMarkerChannel(label=f"{label}-3m1", channel=2, instrument=label, translator="APSPattern", channel_db=self.channelDatabase)
        m4     = Channels.PhysicalMarkerChannel(label=f"{label}-4m1", channel=3, instrument=label, translator="APSPattern", channel_db=self.channelDatabase)

        this_transmitter = Channels.Transmitter(label=label, model="APS", address=address, channels=[chan1, chan2, m1, m2, m3, m4], channel_db=self.channelDatabase)
        this_transmitter.trigger_source = "external"
        this_transmitter.address        = address

        self.add_and_update_dict(this_transmitter)
        return this_transmitter

    @check_for_duplicates
    def new_TDM(self, label, address, **kwargs):
        return Channels.Processor(label=label, model="TDM", address=address, trigger_interval=250e-6)

    @check_for_duplicates
    def new_spectrum_analzyer(self, label, address, source, **kwargs):
        sa = Channels.SpectrumAnalyzer(label=label, model="SpectrumAnalyzer", address=address, LO_source=source, channel_db=self.channelDatabase, **kwargs)
        self.add_and_update_dict(sa)
        return sa

    @check_for_duplicates
    def new_DC_source(self, label, address, **kwargs):
        dcsource = Channels.DCSource(label=label, model="YokogawaGS200", address=address, standalone=True, channel_db=self.channelDatabase, **kwargs)
        self.add_and_update_dict(dcsource)
        return dcsource

    @check_for_duplicates
    def new_attenuator(self,label,address,attenuation=0):
        chan1 = Channels.AttenuatorChannel(label=f"AttenChan-{label}-1", channel=1, attenuation=attenuation, channel_db=self.channelDatabase)
        chan2 = Channels.AttenuatorChannel(label=f"AttenChan-{label}-2", channel=2, attenuation=attenuation, channel_db=self.channelDatabase)
        chan3 = Channels.AttenuatorChannel(label=f"AttenChan-{label}-3", channel=3, attenuation=attenuation, channel_db=self.channelDatabase)
        thing = Channels.Attenuator(label=label,model="DigitalAttenuator",address=address,channels=[chan1, chan2, chan3], standalone=True, channel_db=self.channelDatabase)
        self.add_and_update_dict(thing)
        return thing

    @check_for_duplicates
    def new_APS2_rack(self, label, ip_addresses, tdm_ip=None, **kwargs):
        transmitters  = [self.new_APS2(f"{label}_U{n+1}", f"{ip}") for n, ip in enumerate(ip_addresses)]
        this_transceiver = Channels.Transceiver(label=label, model="APS2Rack", master=True, address=ip_addresses[0], transmitters=transmitters, channel_db=self.channelDatabase, **kwargs)
        for t in transmitters:
            t.transceiver = this_transceiver

        if tdm_ip:
            tdm = self.new_TDM(f"{label}_TDM", tdm_ip)
            this_transceiver.processors = [tdm]
            for t in transmitters:
                t.trigger_source = 'system'

        self.add_and_update_dict(this_transceiver)
        return this_transceiver

    @check_for_duplicates
    def new_X6(self, label, address, dsp_channel=0, record_length=1024, **kwargs):

        phys_channels = (1, 2)
        chans = []

        for phys_chan in (1,2):
            chans.append(Channels.ReceiverChannel(label=f"RecvChan-{label}-{phys_chan}",
                            channel=phys_chan, channel_db=self.channelDatabase))

        this_receiver = Channels.Receiver(label=label, model="X6", address=address, channels=chans,
                                      record_length=record_length, channel_db=self.channelDatabase, **kwargs)
        this_receiver.trigger_source = "external"
        this_receiver.stream_types   = "raw, demodulated, integrated"
        this_receiver.address        = address
        this_receiver.stream_sel     = "X6StreamSelector"

        self.add_and_update_dict(this_receiver)
        return this_receiver

    @check_for_duplicates
    def new_Alazar(self, label, address, record_length=1024, **kwargs):
        chan1 = Channels.ReceiverChannel(label=f"RecvChan-{label}-1", channel=1, channel_db=self.channelDatabase)
        chan2 = Channels.ReceiverChannel(label=f"RecvChan-{label}-2", channel=2, channel_db=self.channelDatabase)

        this_receiver = Channels.Receiver(label=label, model="AlazarATS9870", address=address, channels=[chan1, chan2],
                                      record_length=record_length, channel_db=self.channelDatabase, **kwargs)
        this_receiver.trigger_source = "external"
        this_receiver.stream_types   = "raw"
        this_receiver.address        = address
        this_receiver.stream_sel     = "AlazarStreamSelector"

        self.add_and_update_dict(this_receiver)
        return this_receiver

    @check_for_duplicates
    def new_qubit(self, label, **kwargs):
        thing = Channels.Qubit(label=label, channel_db=self.channelDatabase, **kwargs)
        self.add_and_update_dict(thing)
        return thing

    @check_for_duplicates
    def new_marker(self, label, phys_chan, **kwargs):
        thing = Channels.LogicalMarkerChannel(label=label, phys_chan = phys_chan, channel_db=self.channelDatabase, **kwargs)
        self.add_and_update_dict(thing)
        return thing

    @check_for_duplicates
    def new_source(self, label, model, address, power=-30.0, frequency=5.0e9, reference='10MHz', **kwargs):
        thing = Channels.Generator(label=label, model=model, address=address, power=power,
                                    frequency=frequency, reference=reference,
                                    channel_db=self.channelDatabase, **kwargs)
        self.add_and_update_dict(thing)
        return thing

    def set_control(self, qubit_or_edge, transmitter, generator=None):

        if isinstance(transmitter, Channels.Transmitter):
            quads   = [c for c in transmitter.channels if isinstance(c, Channels.PhysicalQuadratureChannel)]
            markers = [c for c in transmitter.channels if isinstance(c, Channels.PhysicalMarkerChannel)]
            if len(quads) > 1:
            	raise ValueError("In set_control the Transmitter must have a single quadrature channel or a specific channel must be passed instead")
            elif len(quads) == 1:
            	phys_chan = quads[0]
        elif isinstance(transmitter, Channels.PhysicalQuadratureChannel):
            phys_chan = transmitter
            markers = [c for c in transmitter.transmitter.channels if isinstance(c, Channels.PhysicalMarkerChannel)]
        else:
            raise ValueError("In set_control the Transmitter must have a single quadrature channel or a specific channel must be passed instead")

        qubit_or_edge.phys_chan = phys_chan
        if generator:
            qubit_or_edge.phys_chan.generator = generator
        self.update_channelDict()

    def new_edge(self, source, target):
        label = f"{source.label}->{target.label}"
        if label in self.channelDict:
            edge = self.channelDict[f"{source.label}->{target.label}"]
            logger.warning(f"The edge {source.label}->{target.label} already exists: using this edge.")
        else:
            edge = Channels.Edge(label=f"{source.label}->{target.label}", source=source, target=target, channel_db=self.channelDatabase)
        self.add_and_update_dict(edge)
        return edge

    def set_qubit_connectivity(self, graph):
        """
        Graph is a networkx DiGraph consisting of edges (source qubit, target qubit)
        """
        new_edges = [Channels.Edge(label=f"{source.label}->{target.label}", source=source, target=target) for source, target in graph.edges()]
        self.add_and_update_dict(new_edges)
        return new_edges

    def set_measure(self, qubit, transmitter, receivers, generator=None, trig_channel=None, gate=False, gate_channel=None, trigger_length=1e-7):

        if isinstance(transmitter, Channels.Transmitter):
                quads   = [c for c in transmitter.channels if isinstance(c, Channels.PhysicalQuadratureChannel)]
                markers = [c for c in transmitter.channels if isinstance(c, Channels.PhysicalMarkerChannel)]
                if len(quads) > 1:
                    raise ValueError("In set_measure the Transmitter must have a single quadrature channel or a specific channel must be passed instead")
                elif len(quads) == 1:
                    phys_chan = quads[0]
        elif isinstance(transmitter, Channels.PhysicalQuadratureChannel):
            phys_chan = transmitter
            markers = [c for c in transmitter.transmitter.channels if isinstance(c, Channels.PhysicalMarkerChannel)]
        else:
            raise ValueError("In set_measure the Transmitter must have a single quadrature channel or a specific channel must be passed instead")

        if f"M-{qubit.label}" in self.channelDict:
            logger.warning(f"The measurement M-{qubit.label} already exists: using this measurement.")
            meas = self.channelDict[f"M-{qubit.label}"]
        else:
            meas = Channels.Measurement(label=f"M-{qubit.label}", channel_db=self.channelDatabase)
        meas.phys_chan = phys_chan
        if generator:
            meas.phys_chan.generator = generator

        phys_trig_channel = trig_channel if trig_channel else transmitter.get_chan("m1")

        if f"ReceiverTrig-{qubit.label}" in self.channelDict:
            logger.warning(f"The Receiver trigger ReceiverTrig-{qubit.label} already exists: using this channel.")
            trig_chan = self.channelDict[f"ReceiverTrig-{qubit.label}"]
        else:
            trig_chan = Channels.LogicalMarkerChannel(label=f"ReceiverTrig-{qubit.label}", channel_db=self.channelDatabase)
            self.session.add(trig_chan)
        trig_chan.phys_chan    = phys_trig_channel
        trig_chan.pulse_params = {"length": trigger_length, "shape_fun": "constant"}
        meas.trig_chan         = trig_chan
        qubit.measure_chan     = meas

        if isinstance(receivers, Channels.Receiver) and len(receivers.channels) > 1:
            raise ValueError("In set_measure the Receiver must have a single receiver channel or a specific channel must be passed instead")
        elif isinstance(receivers, Channels.Receiver) and len(receivers.channels) == 1:
            rcv_chan = receivers.channels[0]
        elif isinstance(receivers, Channels.ReceiverChannel):
            rcv_chan = receivers
        else:
            raise ValueError("In set_measure the Transmitter must have a single quadrature channel or a specific channel must be passed instead")

        meas.receiver_chan = rcv_chan
        self.add_and_update_dict([meas, trig_chan])

        if gate:
            phys_gate_channel   = gate_channel if gate_channel else transmitter.get_chan("m2")
            if f"M-{qubit.label}-gate" in self.channelDict:
                logger.warning(f"The gate channel M-{qubit.label}-gate already exists: using this channel.")
                gate_chan = self.channelDict[f"M-{qubit.label}-gate"]
            gate_chan           = Channels.LogicalMarkerChannel(label=f"M-{qubit.label}-gate", channel_db=self.channelDatabase)
            gate_chan.phys_chan = phys_gate_channel
            meas.gate_chan      = gate_chan
            self.add_and_update_dict([gate_chan])

    def set_master(self, master_instrument, trig_channel=None, pulse_length=1e-7):

        if isinstance(master_instrument, Channels.Processor):
            master_instrument.master = True

        elif trig_channel:

            if not isinstance(trig_channel, Channels.PhysicalMarkerChannel):
                raise ValueError("In set_master the trigger channel must be an instance of PhysicalMarkerChannel")

            if "slave_trig" in self.channelDict:
                logger.warning(f"The slave trigger slave_trig already exists: using this trigger.")
                st = self.channelDict["slave_trig"]
            else:
                st = Channels.LogicalMarkerChannel(label="slave_trig", channel_db=self.channelDatabase)
            st.phys_chan = trig_channel
            st.pulse_params = {"length": pulse_length, "shape_fun": "constant"}
            master_instrument.master = True
            master_instrument.trigger_source = "internal"
            self.add_and_update_dict([st])

        else:
            raise ValueError(f"Could not determine which transmitter to set as master for {transmitter}:{trig_channel}")

def QubitFactory(label):
    ''' Return a saved qubit channel'''
    channelLib.update_channelDict()
    cs = [c for c in channelLib.channelDatabase.channels if c.label==label]
    # q = channelLib.session.query(Channels.Qubit).filter(Channels.Qubit.label==label and Channels.Qubit.channel_db==channelLib.channelDatabase).all()
    if len(cs) == 1:
        return cs[0]
    else:
        raise Exception(f"Expected to find a single qubit {label} but found {len(cs)} qubits with the same label instead.")

def MeasFactory(label):
    ''' Return a saved measurement channel or create a new one. '''
    channelLib.update_channelDict()
    cs = [c for c in channelLib.channelDatabase.channels if c.label==label]
    # q = channelLib.session.query(Channels.Qubit).filter(Channels.Qubit.label==label and Channels.Qubit.channel_db==channelLib.channelDatabase).all()
    if len(cs) == 1:
        return cs[0]
    else:
        raise Exception(f"Expected to find a single measurement {label} but found {len(cs)} measurements with the same label instead.")

def MarkerFactory(label):
    ''' Return a saved Marker channel or create a new one. '''
    cs = [c for c in channelLib.channelDatabase.channels if c.label==label]
    channelLib.update_channelDict()
    # q = channelLib.session.query(Channels.Qubit).filter(Channels.Qubit.label==label and Channels.Qubit.channel_db==channelLib.channelDatabase).all()
    if len(cs) == 1:
        return cs[0]
    else:
        raise Exception(f"Expected to find a single marker {label} but found {len(cs)} markers with the same label instead.")

def EdgeFactory(source, target):
    channelLib.update_channelDict()
    if channelLib.connectivityG.has_edge(source, target):
        return channelLib.connectivityG[source][target]['channel']
    elif channelLib.connectivityG.has_edge(target, source):
        return channelLib.connectivityG[target][source]['channel']
    else:
        raise ValueError('Edge {0} not found in connectivity graph'.format((
            source, target)))
