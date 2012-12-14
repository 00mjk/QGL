'''
functions for compiling lists of pulses/pulseBlocks down to the hardware level.
'''

import numpy as np

def TekChannels():
    '''
    The set of empty channels for a Tektronix AWG
    '''
    return {'ch1':[], 'ch2':[], 'ch3':[], 'ch4':[], 'ch1m1':[], 'ch1m2':[], 'ch2m1':[], 'ch2m2':[], 'ch3m1':[], 'ch3m2':[] , 'ch4m1':[], 'ch4m2':[]}

def APSChannels():
    '''
    The set of empty channels for a BBN APS.
    '''
    return {chanStr:{'LLs':[], 'WFLibrary':{0:np.zeros(1)}} for chanStr in  ['ch1','ch2','ch3','ch4']}

class LLElement(object):
    def __init__(self, pulse=None):
        self.repeat = 1
        self.isTimeAmp = False
        self.hasTrigger = False
        self.triggerDelay1 = 0
        self.triggerDelay2 = 0
        
        if pulse is None:
            self.key = None
            self.length = 0
            self.phase = 0
            self.frameChange = 0
        else:
            self.key = hash_pulse(pulse.shape)
            self.length = len(pulse.shape)
            self.phase = pulse.phase
            self.frameChange = pulse.frameChange

def compile_to_hardware(seqs):
    linkLists, wfLib = compile_sequences(seq)
    # map logical to physical channels

    # aligns channels to fixed points
    # delays
    # mixer corrects
    # fills empty channels with zeros

    # convert to hardware formats

def compile_sequences(seqs):
    '''
    Main function to convert sequences to miniLL's and waveform libraries.
    '''
    if isinstance(seqs[0], list):
        # nested sequences
        wfLib = {}
        # use seqs[0] as prototype for finding channels (assume every miniLL operates on the same set of channels)
        miniLL, wfLib = compile_sequence(seqs[0], wfLib)
        linkLists = {chan: [LL] for chan, LL in miniLL.items()}
        for seq in seqs[1:]:
            miniLL, wfLib = compile_sequence(seq, wfLib)
            for chan in linkLists.keys():
                linkLists[chan].append(miniLL[chan])
    else:
        miniLL, wfLib = compile_sequence(seq)
        linkLists = {chan: [LL] for chan, LL in miniLL.items()}

    return linkLists, wfLib

def compile_sequence(seq, wfLib = {} ):
    '''
    Converts a single sequence into a miniLL and waveform library.
    Returns a single-entry list of a miniLL and the updated wfLib
    '''
    # normalize sequence to PulseBlocks
    seq = [p.promote() for p in seq]

    #Find the set of logical channels used here and initialize them
    channels = find_unique_channels(seq)

    logicalLLs = {}        
    for chan in channels:
        logicalLLs[chan] = []
        if chan not in wfLib:
            wfLib[chan] = {TAZKey:  np.zeros(1, dtype=np.complex)}

    for block in seq:
        #Align the block 
        blockLength = block.maxPts
        # drop length 0 blocks
        if blockLength == 0:
            continue
        for chan in channels:
            if chan in block.pulses.keys():
                # add aligned LL entry
                wf, LLentry = align(block.pulses[chan], blockLength, block.alignment)
                if hash_pulse(wf) not in wfLib:
                    wfLib[chan][hash_pulse(wf)] = wf
                logicalLLs[chan] += LLentry
            else:
                # add identity
                logicalLLs[chan] += [create_padding_LL(blockLength)]

    # loop through again to find phases, frame changes, and SSB modulation
    for chan, miniLL in logicalLLs.items():
        curFrame = 0
        for entry in miniLL:
            # frame update
            shape = np.copy(wfLib[chan][entry.key])

            # See if we can turn into a TA pair
            # fragile: if you buffer a square pulse it will not be constant valued
            if np.all(shape == shape[0]):
                entry.isTimeAmp = True
                shape = shape[:1]

            shape *= np.exp(1j*(entry.phase+curFrame))
            # TODO SSB modulate
            shapeHash = hash(tuple(shape))
            if shapeHash not in wfLib[chan]:
                wfLib[chan][shapeHash] = shape
            entry.key = shapeHash
            curFrame += entry.frameChange

    # for chan in logicalLLs.keys():
    #     # convert to single-element list
    #     logicalLLs[chan] = [logicalLLs[chan]]

    return logicalLLs, wfLib

def find_unique_channels(seq):
    channels = set([])
    for step in seq:
        channels |= set(step.pulses.keys())
    return channels

def hash_pulse(shape):
    return hash(tuple(shape))

TAZKey = hash_pulse(np.zeros(1, dtype=np.complex))

def create_padding_LL(length):
    tmpLL = LLElement()
    tmpLL.isTimeAmp = True
    tmpLL.key = TAZKey
    tmpLL.length = length
    return tmpLL

def align(pulse, blockLength, alignment, cutoff=12):
    entry = LLElement(pulse)
    entry.length = blockLength
    entry.key = hash_pulse(pulse.shape)
    entry.phase = pulse.phase
    entry.frameChange = pulse.frameChange
    padLength = blockLength - pulse.shape.size
    shape = pulse.shape
    if padLength == 0:
        # can do everything with a single LLentry
        return shape, [entry]
    if (padLength < cutoff) and (alignment == "left" or alignment == "right"):
        # pad the shape on one side
        if alignment == "left":
            shape = np.hstack((shape, np.zeros(padLength)))
        else: #right alignment
            shape = np.hstack((np.zeros(padLength), shape))
        entry.key = hash_pulse(shape)
        return shape, [entry]
    elif (padLength < 2*cutoff and alignment == "center"):
        # pad the shape on each side
        shape = np.hstack(( np.zeros(np.floor(padLength/2)), shape, np.zeros(np.ceil(padLength/2)) ))
        entry.key = hash_pulse(shape)
        return shape, [entry]
    else:
        #split the entry into the shape and one or more TAZ
        if alignment == "left":
            padEntry = create_padding_LL(padLength)
            return shape, [entry, padEntry]
        elif alignment == "right":
            padEntry = create_padding_LL(padLength)
            return shape, [padEntry, entry]
        else:
            padEntry1 = create_padding_LL(np.floor(padLength/2))
            padEntry2 = create_padding_LL(np.ceil(padLength/2))
            return shape, [padEntry1, entry, padEntry2]
