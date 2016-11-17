# Multicut Pipeline implemented with luigi
# Tasks for providing the input data

import luigi
from customTargets import HDF5VolumeTarget, StackedRagTarget

from pipelineParameter import PipelineParameter
from toolsLuigi import config_logger

import logging

import numpy as np
import vigra
import nifty
import os
import h5py
import time

from concurrent import futures

# init the workflow logger
workflow_logger = logging.getLogger(__name__)
config_logger(workflow_logger)


class InputData(luigi.Task):
    """
    Task for loading external input data, e.g. raw data or probability maps.
    For HDF5 input.
    """

    path = luigi.Parameter()
    key  = luigi.Parameter(default = "data")
    # the dtype, should either be float32 or uint8
    dtype = luigi.Parameter(default = "float32")

    def run(self):
        pass


    def output(self):
        """
        Returns the target output.

        :return: Target output
        :rtype: object( :py:class: HDF5Target)
        """

        with h5py.File(self.path, 'r') as f:
            assert self.key in f.keys(), self.key + " , " + f.keys()
            dset = f[self.key]

            if np.dtype(self.dtype) != np.dtype(dset.dtype):
                workflow_logger.debug("InputData task, loading data from %s" % (self.path,) )
                workflow_logger.debug("Changing dtype from %s to %s" % (self.dtype,dset.dtype) )
                self.dtype = dset.dtype

        return HDF5VolumeTarget(self.path, self.dtype, self.key)



class ExternalSegmentation(luigi.Task):
    """
    Task for loading external segmentation from HDF5.
    """

    # Path to the segmentation
    path = luigi.Parameter()
    key  = luigi.Parameter(default = "data")
    # the dtype, should either be uint32 or uint64
    dtype = luigi.Parameter(default = "uint32")

    def run(self):
        pass


    def output(self):
        """
        Returns the target output.

        :return: Target output
        :rtype: object( :py:class: HDF5Target)
        """

        assert os.path.exists(self.path), self.path
        with h5py.File(self.path, 'r') as f:
            assert self.key in f.keys(), self.key + " , " + f.keys()
            dset = f[self.key]

            if np.dtype(self.dtype) != np.dtype(dset.dtype):
                workflow_logger.debug("InputData task, loading data from %s" % (self.path,) )
                workflow_logger.debug("Changing dtype from %s to %s" % (self.dtype,dset.dtype) )
                self.dtype = dset.dtype

        return HDF5VolumeTarget(self.path, self.dtype, self.key)



class ExternalSegmentationLabeled(luigi.Task):
    """
    Task for loading external segmentation from HDF5.
    Perform a label Volume and cache.
    """

    # Path to the segmentation
    pathToSeg = luigi.Parameter()
    keyToSeg  = luigi.Parameter(default = "data")
    # the dtype, should either be uint32 or uint64
    dtype = luigi.Parameter(default = "uint32")

    def requires(self):
        return ExternalSegmentation(self.pathToSeg, self.keyToSeg, self.dtype )

    def run(self):

        segIn = self.input()
        segIn.open()

        shape = segIn.shape

        def labelSlice(segIn, segOut, z):
            begin = [z,0, 0]
            end   = [z+1,shape[1],shape[2]]
            segSlice = segIn.read(begin,end).squeeze()
            segSlice = vigra.analysis.labelImage(segSlice) - 1
            offset  = segSlice.max()
            segOut.write(begin, segSlice[None,:,:])
            return offset

        segOut = self.output()
        segOut.open(shape)

        nWorkers = min( shape[0], PipelineParameter().nThreads )
        #nWorkers = 1
        with futures.ThreadPoolExecutor(max_workers=nWorkers) as executor:
            tasks = []
            for z in xrange(shape[0]):
                tasks.append( executor.submit(labelSlice, segIn, segOut, z) )

        # calculate the offsets for every slice
        offsets = np.array( [task.result() for task in tasks], dtype = np.uint32 )
        offsets = np.cumsum(offsets)
        # need to shift by 1 to the left and insert a 0
        offsets = np.roll(offsets,1)
        offsets[0] = 0

        def addOffset(seg, offset, z):
            begin = [z, 0, 0]
            end   = [z+1,shape[1],shape[2]]
            segSlice = seg.read(begin,end)
            segSlice += offset
            seg.write(begin, segSlice)
            return True


        with futures.ThreadPoolExecutor(max_workers=nWorkers) as executor:
            tasks = []
            for z in xrange(shape[0]):
                tasks.append( executor.submit(addOffset, segOut, offsets[z], z) )

        res = [task.result() for task in tasks]



    def output(self):
        f = h5py.File(self.path, 'r')
        assert self.key in f.keys(), self.key + " , " + f.keys()
        dset = f[self.key]

        if np.dtype(self.dtype) != np.dtype(dset.dtype):
            workflow_logger.debug("InputData task, loading data from %s" % (self.path,) )
            workflow_logger.debug("Changing dtype from %s to %s" % (self.dtype,dset.dtype) )
            self.dtype = dset.dtype

        save_path = os.path.join( PipelineParameter().cache,
                os.path.split(self.pathToSeg)[1][:-3] + "_labeled.h5" )
        return HDF5VolumeTarget( save_path, self.dtype )



class DenseGroundtruth(luigi.Task):
    """
    Task for loading external groundtruth from HDF5.
    """

    path = luigi.Parameter()
    key  = luigi.Parameter(default = "data")
    # the dtype, should either be uint32 or uint64
    dtype = luigi.Parameter(default = np.uint32)

    def requires(self):
        return ExternalSegmentation(self.path, self.key, self.dtype )

    def run(self):

        gt = self.input()
        gt.open()
        # FIXME this is only feasible for small enough data, for larger data we need blockwise connected components
        gt_labeled = vigra.analysis.labelVolumeWithBackground( gt.read([0,0,0], gt.shape) )

        out = self.output()
        out.open(gt.shape)
        out.write( [0,0,0], gt_labeled)

    def output(self):
        save_path = os.path.join( PipelineParameter().cache, os.path.split(self.path)[1][:-3] + "_labeled.h5" )
        return HDF5VolumeTarget( save_path, self.dtype )


class StackedRegionAdjacencyGraph(luigi.Task):
    """
    Task for building the RAG
    """

    pathToSeg = luigi.Parameter()
    keyToSeg = luigi.Parameter(default = "data")

    # not really necessary right now, but maybe the rag syntax will change
    def requires(self):
        return ExternalSegmentation(self.pathToSeg)


    def run(self):

        # get the number of labels
        seg = self.input()

        seg.open()
        shape = seg.shape

        print shape

        seg_last = seg.read( [shape[0]-1,0,0], shape )

        n_labels = seg_last.max() + 1

        print "NumberOfLabels:", n_labels

        #print
        #print "Cache Settings:"
        #print nifty.hdf5.getCacheSettings(seg.get())
        #print

        t_rag = time.time()
        rag = nifty.graph.rag.gridRagStacked2DHdf5( seg.get(), n_labels, numberOfThreads = -1) # nThreads = -1, could also make this accessible
        t_rag = time.time() - t_rag

        workflow_logger.info("Computed RAG in " + str(t_rag) + " s")

        self.output().write( rag, self.pathToSeg, self.keyToSeg)


    def output(self):
        segFile = os.path.split(self.pathToSeg)[1][:-3]
        save_path = "StackedRegionAdjacencyGraph_%s.h5" % (segFile,)
        return StackedRagTarget( os.path.join(PipelineParameter().cache, save_path) )