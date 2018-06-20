'''
Channels is where we store information for mapping virtual (qubit) channel to
real channels.

Split from Channels.py on Jan 14, 2016.
Moved to pony ORM from atom June 1, 2018

Original Author: Colm Ryan
Modified By: Graham Rowlands

Copyright 2016 Raytheon BBN Technologies

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
from pony.orm import *
import networkx as nx

from . import config
from . import Channels
from . import PulseShapes

channelLib = None

def set_from_dict(obj, settings):
    for prop_name in obj.to_dict().keys():
        if prop_name in settings.keys():
            try:
                setattr(obj, prop_name, settings[prop_name])
            except Exception as e:
                print(f"{obj.label}: Error loading {prop_name} from config")

def copy_objs(*entities, new_channel_db):
    # Entities is a list of lists of entities of specific types
    new_entities    = []
    old_to_new      = {}
    links_to_change = {}

    for ent in entities:
        new_ents = []
        for obj in ent:
            c, links = copy_entity(obj, new_channel_db)
            new_ents.append(c)
            links_to_change[c] = links
            old_to_new[c.label] = c
        new_entities.append(new_ents)

    for chan, link_info in links_to_change.items():
        for attr_name, link_name in link_info.items():
            if isinstance(link_name, pony.orm.core.Multiset):
                new = [old_to_new[ln] for ln in link_name]
            else:
                new = old_to_new[link_name]
            setattr(chan, attr_name, new)

    return new_entities

def copy_entity(obj, new_channel_db):
    """Copy a pony entity instance"""
    kwargs = {a.name: getattr(obj, a.name) for a in obj._attrs_ if a.name not in ["id", "classtype"]}

    # Extract any links to other entities
    links = {}
    for attr in obj._attrs_:
        if attr.name not in ["channel_db"]:
            obj_attr = getattr(obj, attr.name)
            if hasattr(obj_attr, "id"):
                kwargs.pop(attr.name)
                links[attr.name] = obj_attr.label 

    kwargs["channel_db"] = new_channel_db
    return obj.__class__(**kwargs), links

class ChannelLibrary(object):

    def __init__(self, database_file=None, channelDict={}, **kwargs):
        """Create the channel library."""

        global channelLib
        if channelLib is not None:
            channelLib.db.disconnect()

        config.load_db()
        if database_file:
            self.database_file = database_file
        elif config.db_file:
            self.database_file = config.db_file
        else:
            self.database_file = ":memory:"

        self.db = Database()
        Channels.define_entities(self.db)
        self.db.bind('sqlite', filename=self.database_file, create_db=True)
        self.db.generate_mapping(create_tables=True)

        # Dirty trick: push the correct entity defs to the calling context
        for var in ["Measurement","Qubit","Edge"]:
            inspect.stack()[1][0].f_globals[var] = getattr(Channels, var)

        self.connectivityG = nx.DiGraph()
        
        # This is still somewhere legacy QGL behavior. Massage db into dict for lookup.
        self.channelDict = {}

        # Check to see whether there is already a temp database
        for cdb in select(d for d in Channels.ChannelDatabase if d.label == "__temp__"):
            self.clear(channel_db=cdb, create_new=False)

        self.channelDatabase = Channels.ChannelDatabase(label="__temp__", time=datetime.datetime.now())
        commit()

        config.load_config()

        # Update the global reference
        channelLib = self

    def get_current_channels(self):
        return list(self.channelDatabase.channels) + list(self.channelDatabase.sources)

    def update_channelDict(self):
        self.channelDict = {c.label: c for c in self.get_current_channels()}

    def ls(self):
        select((c.label, c.time, c.id) for c in Channels.ChannelDatabase if c.label != "__temp__").sort_by(1, 2).show()

    def ent_by_type_name(self, name, show=False):
        q = select(c for c in getattr(Channels,name) if c.label != "__temp__")
        if show:
            select(c.label for c in getattr(Channels,name) if c.label != "__temp__").sort_by(1).show()
        else:
            return {el.label: el for el in q}

    def dig(self):
        return self.ent_by_type_name("Digitizer")

    def awg(self):
        return self.ent_by_type_name("AWG")

    def qubit(self):
        return self.ent_by_type_name("Qubit")

    def meas(self):
        return self.ent_by_type_name("Measurement")

    def ls_dig(self):
        return self.ent_by_type_name("Digitizer", show=True)

    def ls_awg(self):
        return self.ent_by_type_name("AWG", show=True)

    def ls_qubit(self):
        return self.ent_by_type_name("Qubit", show=True)

    def ls_meas(self):
        return self.ent_by_type_name("Measurement", show=True)

    def load(self, name, index=1):
        """Load the latest instance for a particular name. Specifying index = 2 will select the second most recent instance """
        obj = list(select(c for c in Channels.ChannelDatabase if c.label==name).sort_by(desc(Channels.ChannelDatabase.time)))
        self.load_obj(obj[-index])

    def load_by_id(self, id_num):
        obj = select(c for c in Channels.ChannelDatabase if c.id==id_num).first()
        self.load_obj(obj)

    def clear(self, channel_db=None, create_new=True):
        # If no database is specified, clear self.database
        channel_db = channel_db if channel_db else self.channelDatabase
        # First clear items that don't have Sets of other items
        for ent in [Channels.MicrowaveSource, Channels.Channel, Channels.AWG, Channels.Digitizer]:
            select(c for c in ent if c.channel_db == channel_db).delete(bulk=True)
            commit()        
        # Now clear items that do potentially have sets of items (which should be deleted)
        for ent in [Channels.ChannelDatabase]:
            select(d for d in ent if d.label == "__temp__").delete(bulk=True)
            commit()
        if create_new:
            self.channelDatabase = Channels.ChannelDatabase(label="__temp__", time=datetime.datetime.now())
            commit()

    def load_obj(self, obj):
        commit()
        self.clear()
        chans, srcs, awgs, digs = map(list, [obj.channels, obj.sources, obj.awgs, obj.digitizers])
        copy_objs(chans, srcs, awgs, digs, new_channel_db=self.channelDatabase)
        commit()
        self.update_channelDict()

    def save_as(self, name):
        chans, srcs, awgs, digs = map(list, [self.channelDatabase.channels, self.channelDatabase.sources,
                                            self.channelDatabase.awgs, self.channelDatabase.digitizers])
        commit()
        cd = Channels.ChannelDatabase(label=name, time=datetime.datetime.now())
        new_chans, new_srcs, new_awgs, new_digs = copy_objs(chans, srcs, awgs, digs, new_channel_db=cd)
        cd.channels, cd.sources, cd.awgs, cd.digitizers = new_chans, new_srcs, new_awgs, new_digs
        commit()
        
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
        for chan in select(q for q in Channels.Qubit if q not in self.connectivityG):
            self.connectivityG.add_node(chan)
        for chan in select(e for e in Channels.Edge):
            self.connectivityG.add_edge(chan.source, chan.target)
            self.connectivityG[chan.source][chan.target]['channel'] = chan

# Convenience functions for generating and linking channels
# TODO: move these to a shim layer shared by Auspex/QGL

def new_APS2(label, address):
    chan12 = Channels.PhysicalQuadratureChannel(label=f"{label}-12", instrument=label, translator="APS2Pattern", channel_db=channelLib.channelDatabase)
    m1     = Channels.PhysicalMarkerChannel(label=f"{label}-12m1", instrument=label, translator="APS2Pattern", channel_db=channelLib.channelDatabase)
    m2     = Channels.PhysicalMarkerChannel(label=f"{label}-12m2", instrument=label, translator="APS2Pattern", channel_db=channelLib.channelDatabase)
    m3     = Channels.PhysicalMarkerChannel(label=f"{label}-12m3", instrument=label, translator="APS2Pattern", channel_db=channelLib.channelDatabase)
    m4     = Channels.PhysicalMarkerChannel(label=f"{label}-12m4", instrument=label, translator="APS2Pattern", channel_db=channelLib.channelDatabase)
    
    this_awg = Channels.AWG(label=label, address=address, channels=[chan12, m1, m2, m3, m4], channel_db=channelLib.channelDatabase)
    this_awg.trigger_source = "External"
    this_awg.address        = address

    commit()
    return this_awg

def new_X6(label, address):
    chan1 = Channels.ReceiverChannel(label=f"RecvChan-{label}-1", channel_db=channelLib.channelDatabase)
    chan2 = Channels.ReceiverChannel(label=f"RecvChan-{label}-2", channel_db=channelLib.channelDatabase)
    
    this_dig = Channels.Digitizer(label=label, address=address, channels=[chan1, chan2], channel_db=channelLib.channelDatabase)
    this_dig.trigger_source = "External"
    this_dig.address        = address

    commit()
    return this_dig

def new_qubit(label):
    thing = Channels.Qubit(label=label, channel_db=channelLib.channelDatabase)
    commit()
    return thing

def new_source(label, source_type, address, power=-30.0):
    thing = Channels.MicrowaveSource(label=label, source_type=source_type, address=address, power=power, channel_db=channelLib.channelDatabase)
    commit()
    return thing

def set_control(qubit, awg, generator=None):
    quads   = [c for c in awg.channels if isinstance(c, Channels.PhysicalQuadratureChannel)]
    markers = [c for c in awg.channels if isinstance(c, Channels.PhysicalMarkerChannel)]

    if isinstance(awg, Channels.AWG) and len(quads) > 1:
        raise ValueError("In set_control the AWG must have a single quadrature channel or a specific channel must be passed instead")
    elif isinstance(awg, Channels.AWG) and len(quads) == 1:
        phys_chan = quads[0]
    elif isinstance(awg, Channels.PhysicalQuadratureChannel):
        phys_chan = awg
    else:
        raise ValueError("In set_control the AWG must have a single quadrature channel or a specific channel must be passed instead")

    qubit.phys_chan = phys_chan
    if generator:
        qubit.phys_chan.generator = generator
    commit()
    
def set_measure(qubit, awg, dig, generator=None, dig_channel=1, trig_channel=None, gate=False, gate_channel=None, trigger_length=1e-7):
    quads   = [c for c in awg.channels if isinstance(c, Channels.PhysicalQuadratureChannel)]
    markers = [c for c in awg.channels if isinstance(c, Channels.PhysicalMarkerChannel)]

    if isinstance(awg, Channels.AWG) and len(quads) > 1:
        raise ValueError("In set_measure the AWG must have a single quadrature channel or a specific channel must be passed instead")
    elif isinstance(awg, Channels.AWG) and len(quads) == 1:
        phys_chan = quads[0]
    elif isinstance(awg, Channels.PhysicalQuadratureChannel):
        phys_chan = awg
    else:
        raise ValueError("In set_measure the AWG must have a single quadrature channel or a specific channel must be passed instead")

    meas = Channels.Measurement(label=f"M-{qubit.label}", channel_db=channelLib.channelDatabase)
    meas.phys_chan = phys_chan
    if generator:
        meas.phys_chan.generator = generator
    
    phys_trig_channel = trig_channel if trig_channel else awg.get_chan("12m1")

    trig_chan              = Channels.LogicalMarkerChannel(label=f"digTrig-{qubit.label}", channel_db=channelLib.channelDatabase)
    trig_chan.phys_chan    = phys_trig_channel
    trig_chan.pulse_params = {"length": trigger_length, "shape_fun": "constant"}
    meas.trig_chan         = trig_chan
    
    if isinstance(dig, Channels.Digitizer) and len(dig.channels) > 1:
        raise ValueError("In set_measure the Digitizer must have a single receiver channel or a specific channel must be passed instead")
    elif isinstance(dig, Channels.Digitizer) and len(dig.channels) == 1:
        rcv_chan = dig.channels[0]
    elif isinstance(dig, Channels.ReceiverChannel):
        rcv_chan = dig
    else:
        raise ValueError("In set_measure the AWG must have a single quadrature channel or a specific channel must be passed instead")

    meas.receiver_chan = rcv_chan

    if gate:
        phys_gate_channel   = gate_channel if gate_channel else awg.get_chan("12m2")
        gate_chan           = Channels.LogicalMarkerChannel(label=f"M-{qubit.label}-gate", channel_db=channelLib.channelDatabase)
        gate_chan.phys_chan = phys_gate_channel
        meas.gate_chan      = gate_chan
    commit()
        
def set_master(awg, trig_channel, pulse_length=1e-7):
    if not isinstance(trig_channel, Channels.PhysicalMarkerChannel):
        raise ValueError("In set_master the trigger channel must be an instance of PhysicalMarkerChannel")
   
    st = Channels.LogicalMarkerChannel(label="slave_trig", channel_db=channelLib.channelDatabase)
    st.phys_chan = trig_channel
    st.pulse_params = {"length": pulse_length, "shape_fun": "constant"}
    awg.master = True
    awg.trigger_source = "Internal"
    commit()

def QubitFactory(label, **kwargs):
    ''' Return a saved qubit channel or create a new one. '''
    # TODO: this will just get the first entry in the whole damned DB!
    # thing = select(el for el in Channels.Qubit if el.label==label).first()
    thing = {c.label: c for c in channelLib.get_current_channels()}[label]
    if thing:
        return thing
    else:
        return Channels.Qubit(label=label, **kwargs)
    
def MeasFactory(label, **kwargs):
    ''' Return a saved measurement channel or create a new one. '''
    thing = {c.label: c for c in channelLib.get_current_channels()}[label]
    if thing:
        return thing
    else:
        return Channels.Measurement(label=label, **kwargs)

def MarkerFactory(label, **kwargs):
    ''' Return a saved Marker channel or create a new one. '''
    thing = {c.label: c for c in channelLib.get_current_channels()}[label]
    if thing:
        return thing
    else:
        return Channels.LogicalMarkerChannel(label=label, **kwargs)

def EdgeFactory(source, target):
    if channelLib.connectivityG.has_edge(source, target):
        return channelLib.connectivityG[source][target]['channel']
    elif channelLib.connectivityG.has_edge(target, source):
        return channelLib.connectivityG[target][source]['channel']
    else:
        raise ValueError('Edge {0} not found in connectivity graph'.format((
            source, target)))

