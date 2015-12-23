# Copyright (c) 2012-2013, The Linux Foundation. All rights reserved.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 and
# only version 2 as published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

import sys
import re
import os
import struct
import datetime
import array
import string
import bisect
import traceback
from subprocess import *
from optparse import OptionParser
from optparse import OptionGroup
from struct import unpack
from ctypes import *
from tempfile import *
from print_out import *

def cleanupString(str):
    if str is None :
        return str
    else :
        return ''.join([c for c in str if c in string.printable])

cpu_context_save_str="".join([
    "I", #    __u32   r4
    "I", #    __u32   r5
    "I", #    __u32   r6
    "I", #    __u32   r7
    "I", #    __u32   r8
    "I", #    __u32   r9
    "I", #    __u32   sl
    "I", #    __u32   fp 14
    "I", #    __u32   sp 15
    "I", #    __u32   pc 16
    "II", #    __u32   extra[2]               /* Xscale 'acc' register, etc */
    ])

thread_info_str="".join([ #struct thread_info {
    "I", #    flags          /* low level flags */
    "I", #    int                     preempt_count  /* 0 => preemptable, <0 => bug */
    "I", #                addr_limit     /* address limit */
    "I", #     task
    "I", #     exec_domain   /* execution domain */
    "I", #      5                 cpu            /* cpu */
    "I", #                       cpu_domain     /* cpu domain */
    cpu_context_save_str, #    struct cpu_context_save cpu_context    /* cpu context */
    "I", #                       syscall        /* syscall number */
    "I", #    unsigned char                    used_cp[16]    /* thread used copro */
    "I",#               tp_value
    ])

thread_info_cpu_idx = 5
thread_info_fp_idx = 14
thread_info_sp_idx = 15
thread_info_pc_idx = 16

def find_panic(ramdump, unwind, addr_stack, out_dir, thread_task_name) :
    for i in range(addr_stack, addr_stack+0x2000, 4) :
        pc = ramdump.read_word(i)
        lr = ramdump.read_word(i+4)
        spx = ramdump.read_word(i+8)
        l = ramdump.unwind_lookup(pc)
        if l is not None :
            s, offset = l
            if s == "panic" :
                print_out_str ("Faulting process found! Name {0}. Attempting to retrieve stack (sp = {1:x} pc = {2:x})".format(thread_task_name, i+4, pc))
                unwind.unwind_backtrace(i+4, 0, pc, lr, "")
                if out_dir is None :
                    out_dir = ""
                regspanic = open(out_dir+"/regs_panic.cmm","w")
                regspanic.write("r.s pc 0x{0:x}\n".format(pc))
                regspanic.write("r.s r13 0x{0:x}\n".format(i+4))
                regspanic.close()
                return True
    return False

def dump_thread_group(ramdump, unwind, thread_group, task_out, check_for_panic = 0, out_dir = None) :
    offset_thread_group = ramdump.get_offset_struct("((struct task_struct *)0x0)", "thread_group")
    offset_comm = ramdump.get_offset_struct("((struct task_struct *)0x0)", "comm")
    offset_pid = ramdump.get_offset_struct("((struct task_struct *)0x0)", "pid")
    offset_stack = ramdump.get_offset_struct("((struct task_struct *)0x0)", "stack")
    offset_state = ramdump.get_offset_struct("((struct task_struct *)0x0)", "state")
    offset_exit_state = ramdump.get_offset_struct("((struct task_struct *)0x0)", "exit_state")
    orig_thread_group = thread_group
    first = 0
    task_list_count = 0
    while True :
        next_thread_start = thread_group - offset_thread_group
        next_thread_comm = next_thread_start + offset_comm
        next_thread_pid = next_thread_start + offset_pid
        next_thread_stack = next_thread_start + offset_stack
        next_thread_state = next_thread_start + offset_state
        next_thread_exit_state = next_thread_start + offset_exit_state
        thread_task_name = cleanupString(ramdump.read_cstring(next_thread_comm, 16))
        if thread_task_name is None :
            return
        thread_task_pid = ramdump.read_word(next_thread_pid)
        if thread_task_pid is None :
            return
        task_state = ramdump.read_word(next_thread_state)
        if task_state is None :
            return
        task_exit_state = ramdump.read_word(next_thread_exit_state)
        if task_exit_state is None :
            return
        addr_stack = ramdump.read_word(next_thread_stack)
        if addr_stack is None :
            return
        threadinfo = ramdump.read_string(addr_stack, thread_info_str)
        if threadinfo is None:
            return
        if not check_for_panic :
            if not first :
                task_out.write("Process: {0}, cpu: {1} pid: {2} start: 0x{3:x}\n".format(thread_task_name, threadinfo[thread_info_cpu_idx], thread_task_pid, next_thread_start))
                task_out.write("=====================================================\n")
                first = 1
            task_list_count = task_list_count + 1;
            task_out.write("    Task name: {0} pid: {1} cpu: {2}\n    state: 0x{3:x} exit_state: 0x{4:x} stack base: 0x{5:x}\n".format(
                    thread_task_name, thread_task_pid, threadinfo[thread_info_cpu_idx], task_state, task_exit_state, addr_stack))
            task_out.write ("    Stack:")
            unwind.unwind_backtrace(threadinfo[thread_info_sp_idx], threadinfo[thread_info_fp_idx], threadinfo[thread_info_pc_idx], 0, "    ", task_out)
            task_out.write ("=======================================================\n")
        else :
# LGE_CHANGE_S: improve time for check panic - only check for TASK_RUNNING state
# original
#            find_panic(ramdump, unwind, addr_stack, out_dir, thread_task_name)
            if task_state == 0 :
                find_panic(ramdump, unwind, addr_stack, out_dir, thread_task_name)
# LGE_CHANGE_E

        next_thr = ramdump.read_word(thread_group)
        if (next_thr == thread_group) and (next_thr != orig_thread_group) :
            if check_for_panic :
                print_out_str("!!!! Cycle in thread group! The list is corrupt!\n")
            else :
                task_out.write ("!!!! Cycle in thread group! The list is corrupt!\n")
            break

        thread_group = next_thr
        if thread_group == orig_thread_group :
            break

        if ( task_list_count > 1000 ) :
            print ( " (%s) process have a lot of task..!!!!!!!! Aborted ( task count exceed %d ) " %(thread_task_name, task_list_count) )
            print_out_str ( " (%s) process have a lot of task..!!!!!!!! Aborted ( task count exceed %d ) " %(thread_task_name, task_list_count) )
            break

def do_dump_stacks(ramdump, unwind, check_for_panic = 0) :
    offset_tasks = ramdump.get_offset_struct("((struct task_struct *)0x0)", "tasks")
    offset_comm = ramdump.get_offset_struct("((struct task_struct *)0x0)", "comm")
    offset_stack = ramdump.get_offset_struct("((struct task_struct *)0x0)", "stack")
    offset_thread_group = ramdump.get_offset_struct("((struct task_struct *)0x0)", "thread_group")
    offset_pid = ramdump.get_offset_struct("((struct task_struct *)0x0)", "pid")
    offset_state = ramdump.get_offset_struct("((struct task_struct *)0x0)", "state")
    offset_exit_state = ramdump.get_offset_struct("((struct task_struct *)0x0)", "exit_state")
    init_addr = ramdump.addr_lookup("init_task")
    init_next_task = init_addr + offset_tasks
    orig_init_next_task = init_next_task
    init_thread_group = init_addr+offset_thread_group
    out_dir = ramdump.outdir
    if check_for_panic == 0 :
        task_out = open("{0}/tasks.txt".format(out_dir),"wb")
    else :
        task_out = None
    while True :
        dump_thread_group(ramdump, unwind, init_thread_group, task_out, check_for_panic, out_dir)
        next_task = ramdump.read_word(init_next_task)
        if next_task is None :
            return

        if (next_task == init_next_task) and (next_task != orig_init_next_task) :
            if check_for_panic :
                print_out_str("!!!! Cycle in task list! The list is corrupt!\n")
            else :
                task_out.write("!!!! Cycle in task list! The list is corrupt!\n")
            break

        init_next_task = next_task
        init_thread_group = init_next_task - offset_tasks + offset_thread_group
        if init_next_task == orig_init_next_task :
            break
    if check_for_panic == 0 :
        task_out.close()
    print_out_str("---wrote tasks to {0}/tasks.txt".format(out_dir))
