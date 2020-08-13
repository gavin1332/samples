from __future__ import print_function
import numpy as np

import paddle.fluid as fluid
from paddle.fluid import core, layers

if 'program_to_code' in dir(fluid.transpiler.details.program_utils):
  from paddle.fluid.transpiler.details.program_utils import program_to_code


OP_ROLE_VAR_KEY = core.op_proto_and_checker_maker.kOpRoleVarAttrName()

def print_program(program, name='program'):
  with open(name, 'w') as fout:
    if 'program_to_code' in globals():
      program_to_code(program, fout=fout)
    else:
      print(program, file=fout) 


def create_coalesce_program(grad_dict):
  coalesce_program = fluid.Program()
  in_vars = []
  out_vars = []
  with fluid.program_guard(coalesce_program):
    grad_out_dict = {}
    for name in grad_dict:
      grad = grad_dict[name]
      grad_in = layers.fill_constant(shape=grad.shape, dtype='float32', value=0)
      grad_out = layers.create_global_var(name='output_' + grad.name,
          shape=grad.shape, value=0, dtype='float32', persistable=True)
      in_vars.append(grad_in)
      out_vars.append(grad_out)
      grad_out_dict[name] = grad_out
    grad_fused = layers.create_global_var(name='fused_output', shape=[1],
        value=0, dtype='float32', persistable=True)
    coalesce_program.global_block().append_op(type='coalesce_tensor',
        inputs={'Input': in_vars},
        outputs={'Output': out_vars, 'FusedOutput': grad_fused},
        attrs={'copy_data': False, "dtype": core.VarDesc.VarType.FP32})
  return coalesce_program, grad_out_dict, grad_fused


sgd_optimizer = fluid.optimizer.SGD(learning_rate=0.001)
place = fluid.CPUPlace()
exe = fluid.Executor(place)
feed_data = np.random.uniform(0, 1, size=(1, 10)).astype('float32')

start_program = fluid.Program()
main_program = fluid.Program()
with fluid.program_guard(main_program, start_program):
  data = fluid.data(name='data', shape=[-1, 10], dtype='float32')
  fc = layers.fc(data, size=10)
  loss = layers.reduce_sum(fc)

  opt_out = sgd_optimizer.minimize(loss)

print_program(main_program, 'program.before')

grad_dict = {grad.name : grad for param, grad in opt_out[1]}

coalesce_program, grad_out_dict, grad_fused = create_coalesce_program(grad_dict)
print_program(coalesce_program, 'program.coalesce')

with fluid.program_guard(main_program):
  for name in grad_out_dict:
    grad_out = grad_out_dict[name]
    layers.create_global_var(name=grad_out.name, dtype='float32',
        shape=grad_out.shape, value=0, persistable=True)
  layers.create_global_var(name=grad_fused.name, dtype='float32',
      shape=grad_fused.shape, value=0, persistable=True)

for name in grad_dict:
  main_program.global_block()._remove_var(name)

for op in main_program.global_block().ops:
  for out_name in op.output_arg_names:
    if out_name in grad_out_dict:
      op._rename_output(out_name, grad_out_dict[out_name].name)

  if OP_ROLE_VAR_KEY in op.attr_names:
    op_role_var = op.all_attrs()[OP_ROLE_VAR_KEY]
    if len(op_role_var) == 0 or len(op_role_var) % 2 != 0:
      continue

    grad_name = op_role_var[1]
    if grad_name in grad_out_dict:
      op_role_var[1] = grad_out_dict[grad_name].name
      op._update_desc_attr(OP_ROLE_VAR_KEY, op_role_var)

      if grad_name in op.input_arg_names:
        op._rename_input(grad_name, grad_out_dict[grad_name].name)


print_program(main_program, 'program.after')
  
scope = fluid.Scope()
exe.run(start_program, scope=scope)
exe.run(coalesce_program, scope=scope)

loss_val = exe.run(main_program, feed={'data': feed_data}, fetch_list=[loss.name], scope=scope)
print(loss_val)