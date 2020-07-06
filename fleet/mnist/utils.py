import paddle.fluid as fluid
from paddle.fluid.incubate.fleet.collective import fleet


BATCH_SIZE = 16

def create_dataloader(generator, feed, place, is_test, is_distributed):
    # Split dataset into nranks parts, and each rank runs it's own part.
    #
    # There are three main dataset formats:
    #   1. [RECOMMENDED] One sample in one file, and the dataset consists of a file list
    #   2. multiple samples in one file, and the dataset consists of a file list
    #   3. multiple samples in one file, and it is the whole dataset
    # 
    # And the corresponding solutions are:
    #   1. Split a file list into nranks parts uniformly 
    #   2. Split a file list into nranks parts and guarantee that each part consists of
    #      the same amount of samples, otherwise, it may cause hanging while training
    #   3. Parse the dataset first and then split them into nranks parts. The example
    #      below is in this condition, and the redundant parsing is unavoidable 
    #
    # Currently, one way to avoid uneven segmentation is dropping tail samples less than
    # nranks. Another way is training with the whole dataset in each rank with different
    # shuffling. It could keep the amount of samples uniform among all ranks. But keep
    # in mind that the number of samples in ONE pass is equivalent to that of NRANKS
    # passes. What is more, if the total number of training passes is kown as N, we
    # could even replicate the dataset for N times in each rank, and do shuffling and
    # training to utilize the dataset much more efficiently. The small expense of this
    # is losing the explicit boundary information for each pass of training.
    #
    # We could distinguish these two strategies as sampling without replacement and
    # sampleing with replacement.
    def _dist_wrapper(generator):
        def _wrapper():
            rank = fleet.worker_index()
            nranks = fleet.worker_num()
            for idx, sample in enumerate(generator()):
                if idx % nranks == rank:
                    yield sample
        return _wrapper

    if is_distributed:
        generator = _dist_wrapper(generator)

    drop_last = False if is_test else True
    loader = fluid.io.DataLoader.from_generator(feed_list=feed, capacity=16)
    loader.set_sample_generator(generator, batch_size=BATCH_SIZE,
            drop_last=drop_last, places=[place])
    return loader


def dist_eval_acc(exe, local_acc, local_weight):
    prog = fluid.Program()
    with fluid.program_guard(prog):
        acc = fluid.layers.data(name='acc', shape=[1], dtype='float32')
        weight = fluid.layers.data(name='weight', shape=[1], dtype='float32')
        dist_acc = fluid.layers.collective._c_allreduce(acc, reduce_type='sum', use_calc_stream=True)
        dist_weight = fluid.layers.collective._c_allreduce(weight, reduce_type='sum', use_calc_stream=True)
    acc_sum, weight_sum = exe.run(prog, feed={'acc': local_acc, 'weight': local_weight}, fetch_list=[dist_acc, dist_weight]) 
    return acc_sum / weight_sum


def sample_batch(sample):
    tensor = list(sample[0].values())[0]
    assert(isinstance(tensor, fluid.LoDTensor))
    return tensor.shape()[0]

