# Copyright (c) 2013, The Linux Foundation. All rights reserved.
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
import math
import rb_tree
from subprocess import *
from optparse import OptionParser
from optparse import OptionGroup
from struct import unpack
from ctypes import *
from tempfile import *
from print_out import *
import linux_list as llist

IOMMU_DOMAIN_VAR = 'domain_root'

#Keep these in sync with below offset_required_iommu table.
NODE_IDX = 0
POOLS_IDX = 1
NPOOLS_IDX = 2
DOMAIN_IDX = 3
DOMAIN_NUM_IDX = 4
MSM_IOVA_DATA_SIZE_IDX = 5
PRIV_IDX = 6
IOMMU_CTX_ATTACHED_IDX = 7
IOMMU_CTX_NAME_IDX = 8
IOMMU_CTX_NUM_IDX = 9
IOMMU_PRIV_PT_IDX = 10
LIST_ATTACHED_IDX = 11
IOMMU_CLIENT_NAME_IDX = 12
PG_TABLE_IDX = 13
REDIRECT_IDX = 14

#Keep these in sync with above IDX's
offsets_required_iommu = [
        ('((struct msm_iova_data *)0x0)', 'node', 0, 0),
        ('((struct msm_iova_data *)0x0)', 'pools', 0, 0),
        ('((struct msm_iova_data *)0x0)', 'npools', 0, 0),
        ('((struct msm_iova_data *)0x0)', 'domain', 0, 0),
        ('((struct msm_iova_data *)0x0)', 'domain_num', 0, 0),
        ('sizeof(struct msm_iova_data)','',0, 1),
        ('((struct iommu_domain *)0x0)', 'priv', 0, 0),
        ('((struct msm_iommu_ctx_drvdata *)0x0)', 'attached_elm', 0, 0),
        ('((struct msm_iommu_ctx_drvdata *)0x0)', 'name', 0, 0),
        ('((struct msm_iommu_ctx_drvdata *)0x0)', 'num', 0, 0),
        ('((struct msm_iommu_priv *)0x0)', 'pt', 0, 0),
        ('((struct msm_iommu_priv *)0x0)', 'list_attached', 0, 0),
        ('((struct msm_iommu_priv *)0x0)', 'client_name', 0, 0),
        ('((struct msm_iommu_pt *)0x0)', 'fl_table', 0, 0),
        ('((struct msm_iommu_pt *)0x0)', 'redirect', 0, 0),
]

SZ_4K = 0x1000
SZ_64K = 0x10000
SZ_1M = 0x100000
SZ_16M = 0x1000000

MAP_SIZE_STR = [ '4K', '8K', '16K', '32K', '64K',
			'128K', '256K', '512K', '1M', '2M',
			'4M', '8M', '16M']

def get_order(size):
	order = math.log(size,2)
	if (order % 1.0) != 0.0:
		print 'ERROR: Number is not a power of 2: %x' % (size)
		order = 0
	else:
		order -= math.log(SZ_4K, 2)
	return int(order)

class IOMMU(object):
	class Domain(object):
		def __init__(self):
			self.domain_num = -1
			self.pg_table = 0
			self.redirect = 0
			self.ctx_name = ''
			self.client_name = ''

	class FlatMapping(object):
		def __init__(self, virt, phys=-1, type='[]', size=SZ_4K, mapped=False):
			self.virt = virt
			self.phys = phys
			self.mapping_type = type
			self.mapping_size = size
			self.mapped = mapped

	class CollapsedMapping(object):
		def __init__(self, virt_start, virt_end, phys_start=-1, phys_end=-1, type='[]', size=SZ_4K, mapped=False):
			self.virt_start = virt_start
			self.virt_end = virt_end-1
			self.phys_start = phys_start
			self.phys_end = phys_end-1
			self.mapping_type = type
			self.mapping_size = size
			self.mapped = mapped
		def phys_size(self):
			return (self.phys_end - self.phys_start + 1)
		def virt_size(self):
			return (self.virt_end - self.virt_start + 1)

	def __init__(self, ram_dump):
		self.out_file = None
		self.ram_dump = ram_dump
		self.domain_list = []
		self.NUM_FL_PTE = 4096
		self.NUM_SL_PTE = 256

		self.FL_BASE_MASK = 0xFFFFFC00
		self.FL_TYPE_TABLE = (1 << 0)
		self.FL_TYPE_SECT = (1 << 1)
		self.FL_SUPERSECTION = (1 << 18)
		self.FL_AP0 = (1 << 10)
		self.FL_AP1 = (1 << 11)
		self.FL_AP2 = (1 << 15)
		self.FL_SHARED = (1 << 16)
		self.FL_BUFFERABLE = (1 << 2)
		self.FL_CACHEABLE = (1 << 3)
		self.FL_TEX0 = (1 << 12)
		self.FL_NG = (1 << 17)

		self.SL_BASE_MASK_LARGE	= 0xFFFF0000
		self.SL_BASE_MASK_SMALL = 0xFFFFF000
		self.SL_TYPE_LARGE = (1 << 0)
		self.SL_TYPE_SMALL = (2 << 0)
		self.SL_AP0 = (1 << 4)
		self.SL_AP1 = (2 << 4)
		self.SL_AP2 = (1 << 9)
		self.SL_SHARED = (1 << 10)
		self.SL_BUFFERABLE = (1 << 2)
		self.SL_CACHEABLE = (1 << 3)
		self.SL_TEX0 = (1 << 6)
		self.SL_NG = (1 << 11)
		self.ctxdrvdata_name_offset = 0
		self.ctxdrvdata_num_offset = 0
		self.ctx_list = []

		self.ram_dump.setup_offset_table(offsets_required_iommu, True)

		self.node_offset = self.ram_dump.get_offset_struct(offsets_required_iommu[NODE_IDX][0], offsets_required_iommu[NODE_IDX][1])
		self.domain_num_offset = self.ram_dump.get_offset_struct(offsets_required_iommu[DOMAIN_NUM_IDX][0], offsets_required_iommu[DOMAIN_NUM_IDX][1])
		self.domain_offset = self.ram_dump.get_offset_struct(offsets_required_iommu[DOMAIN_IDX][0], offsets_required_iommu[DOMAIN_IDX][1])
		self.priv_offset = self.ram_dump.get_offset_struct(offsets_required_iommu[PRIV_IDX][0], offsets_required_iommu[PRIV_IDX][1])
		self.ctxdrvdata_attached_offset = self.ram_dump.get_offset_struct(offsets_required_iommu[IOMMU_CTX_ATTACHED_IDX][0], offsets_required_iommu[IOMMU_CTX_ATTACHED_IDX][1])
		self.ctxdrvdata_name_offset = self.ram_dump.get_offset_struct(offsets_required_iommu[IOMMU_CTX_NAME_IDX][0], offsets_required_iommu[IOMMU_CTX_NAME_IDX][1])
		self.ctxdrvdata_num_offset = self.ram_dump.get_offset_struct(offsets_required_iommu[IOMMU_CTX_NUM_IDX][0], offsets_required_iommu[IOMMU_CTX_NUM_IDX][1])
		self.priv_pt_offset = self.ram_dump.get_offset_struct(offsets_required_iommu[IOMMU_PRIV_PT_IDX][0], offsets_required_iommu[IOMMU_PRIV_PT_IDX][1])
		self.list_attached_offset = self.ram_dump.get_offset_struct(offsets_required_iommu[LIST_ATTACHED_IDX][0], offsets_required_iommu[LIST_ATTACHED_IDX][1])
		self.client_name_offset = self.ram_dump.get_offset_struct(offsets_required_iommu[IOMMU_CLIENT_NAME_IDX][0], offsets_required_iommu[IOMMU_CLIENT_NAME_IDX][1])
		self.pgtable_offset = self.ram_dump.get_offset_struct(offsets_required_iommu[PG_TABLE_IDX][0], offsets_required_iommu[PG_TABLE_IDX][1])
		self.redirect_offset = self.ram_dump.get_offset_struct(offsets_required_iommu[REDIRECT_IDX][0], offsets_required_iommu[REDIRECT_IDX][1])

		self.list_next_offset, self.list_prev_offset = llist.get_list_offsets(self.ram_dump)

	def fl_offset(va):
		return (((va) & 0xFFF00000) >> 20)

	def sl_offset(va):
		return (((va) & 0xFF000) >> 12)

	def list_func(self, node):
		ctx_drvdata_name_ptr = self.ram_dump.read_word(node + self.ctxdrvdata_name_offset)
		num = self.ram_dump.read_word(node + self.ctxdrvdata_num_offset)

		if ctx_drvdata_name_ptr != 0:
			name = self.ram_dump.read_cstring(ctx_drvdata_name_ptr, 100)
			self.ctx_list.append((num, name))

	def iommu_domain_func(self, node):

		domain_num_addr = (node - self.node_offset) + self.domain_num_offset
		domain_num = self.ram_dump.read_word(domain_num_addr)

		domain_addr = (node - self.node_offset) + self.domain_offset
		domain = self.ram_dump.read_word(domain_addr)

		priv_ptr = self.ram_dump.read_word(domain + self.priv_offset)

		if self.client_name_offset is not None:
			client_name_ptr = self.ram_dump.read_word(priv_ptr + self.client_name_offset)
			if client_name_ptr != 0:
				client_name = self.ram_dump.read_cstring(client_name_ptr, 100)
			else:
				client_name = '(null)'
		else:
			client_name = 'unknown'

		if self.list_attached_offset is not None:
			list_attached = self.ram_dump.read_word(priv_ptr + self.list_attached_offset)
		else:
			list_attached = None

		if self.priv_pt_offset is not None:
			pg_table = self.ram_dump.read_word(priv_ptr + self.priv_pt_offset + self.pgtable_offset)
			redirect = self.ram_dump.read_word(priv_ptr + self.priv_pt_offset + self.redirect_offset)
		else:
			#On some builds we are unable to look up the offsets so hardcode the offsets.
			pg_table = self.ram_dump.read_word(priv_ptr + 0)
			redirect = self.ram_dump.read_word(priv_ptr + 4)

			#Note: On some code bases we don't have this pg_table and redirect in the priv structure (see msm_iommu_sec.c). It only
			#contains list_attached. If this is the case we can detect that by checking whether
			#pg_table == redirect (prev == next pointers of the attached list).
			if pg_table == redirect:
				#This is a secure domain. We don't have access to the page tables.
				pg_table = 0
				redirect = None

		if list_attached is not None and list_attached != 0:
			list_walker = llist.ListWalker(self.ram_dump, list_attached, self.ctxdrvdata_attached_offset, self.list_next_offset, self.list_prev_offset)
			list_walker.walk(list_attached, self.list_func)

		dom = self.Domain()
		dom.domain_num = domain_num
		dom.pg_table = pg_table
		dom.redirect = redirect
		dom.ctx_list = self.ctx_list
		dom.client_name = client_name
		self.ctx_list = []
		self.domain_list.append(dom)

	def print_sl_page_table(self, pg_table):
		sl_pte = pg_table
		for i in range(0,self.NUM_SL_PTE):
			phy_addr = self.ram_dump.read_word(sl_pte, False)
			if phy_addr is not None: #and phy_addr & self.SL_TYPE_SMALL:
				read_write = '[R/W]'
				if phy_addr & self.SL_AP2:
					read_write = '[R]'

				if phy_addr & self.SL_TYPE_SMALL:
					self.out_file.write('SL_PTE[%d] = %x %s\n' % (i, phy_addr & self.SL_BASE_MASK_SMALL, read_write))
				elif phy_addr & self.SL_TYPE_LARGE:
					self.out_file.write('SL_PTE[%d] = %x %s\n' % (i, phy_addr & self.SL_BASE_MASK_LARGE, read_write))
				elif phy_addr != 0:
					self.out_file.write('SL_PTE[%d] = %x NOTE: ERROR [Do not understand page table bits]\n' % (i, phy_addr))
			sl_pte += 4


	def print_page_table(self, pg_table):
		fl_pte = pg_table
		for i in range(0,self.NUM_FL_PTE):
		#for i in range(0,5):
			sl_pg_table_phy_addr = self.ram_dump.read_word(fl_pte)
			if sl_pg_table_phy_addr is not None:
				if sl_pg_table_phy_addr & self.FL_TYPE_TABLE:
					self.out_file.write('FL_PTE[%d] = %x [4K/64K]\n' % (i, sl_pg_table_phy_addr & self.FL_BASE_MASK))
					self.print_sl_page_table(sl_pg_table_phy_addr & self.FL_BASE_MASK)
				elif sl_pg_table_phy_addr & self.FL_SUPERSECTION:
					self.out_file.write('FL_PTE[%d] = %x [16M]\n' % (i, sl_pg_table_phy_addr & 0xFF000000))
				elif sl_pg_table_phy_addr & self.FL_TYPE_SECT:
					self.out_file.write('FL_PTE[%d] = %x [1M]\n' % (i, sl_pg_table_phy_addr & 0xFFF00000))
				elif sl_pg_table_phy_addr != 0:
					self.out_file.write('FL_PTE[%d] = %x NOTE: ERROR [Cannot understand first level page table entry]\n' % (i, sl_pg_table_phy_addr))
			else:
					self.out_file.write('FL_PTE[%d] NOTE: ERROR [Cannot understand first level page table entry]\n' % (i))
			fl_pte += 4

	def get_mapping_info(self, pg_table, index):
		sl_pte = pg_table + (index * 4)
		phy_addr = self.ram_dump.read_word(sl_pte, False)
		current_phy_addr = -1
		current_page_size = SZ_4K
		current_map_type = 0
		status = True
		if phy_addr is not None:
			if phy_addr & self.SL_AP2:
				current_map_type = self.SL_AP2
			if phy_addr & self.SL_TYPE_SMALL:
				current_phy_addr = phy_addr & self.SL_BASE_MASK_SMALL
				current_page_size = SZ_4K
			elif phy_addr & self.SL_TYPE_LARGE:
				current_phy_addr = phy_addr & self.SL_BASE_MASK_LARGE
				current_page_size = SZ_64K
			elif phy_addr != 0:
				current_phy_addr = phy_addr
				status = False

		return (current_phy_addr, current_page_size, current_map_type, status)

	def get_sect_mapping_info(self, addr):
		current_phy_addr = -1
		current_page_size = SZ_4K
		current_map_type = 0
		status = True
		if addr is not None:
			if addr & self.SL_AP2:
				current_map_type = self.SL_AP2
			if addr & self.FL_SUPERSECTION:
				current_phy_addr = addr & 0xFF000000
				current_page_size = SZ_16M
			elif addr & self.FL_TYPE_SECT:
				current_phy_addr = addr & 0xFFF00000
				current_page_size = SZ_1M
			elif addr != 0:
				current_phy_addr = addr
				status = False

		return (current_phy_addr, current_page_size, current_map_type, status)

	def add_flat_mapping(self, mappings, fl_idx, sl_idx, phy_adr, map_type, page_size, mapped):
		virt = (fl_idx << 20) | (sl_idx << 12)
		map_type_str = '[R/W]'
		if map_type == self.SL_AP2:
			map_type_str = '[R]'
		map = self.FlatMapping(virt, phy_adr, map_type_str, page_size, mapped)
		if not mappings.has_key(virt):
			mappings[virt] = map
		else:
			self.out_file.write('[!] WARNING: FL_PTE[%d] SL_PTE[%d] ERROR [Duplicate mapping?]\n' % (fl_idx, sl_idx))
		return mappings

	def add_collapsed_mapping(self, mappings, virt_start, virt_end, phys_start, phys_end, map_type, page_size, mapped):
		map = self.CollapsedMapping(virt_start, virt_end, phys_start, phys_end, map_type, page_size, mapped)
		if not mappings.has_key(virt_start):
			mappings[virt_start] = map
		else:
			self.out_file.write('[!] WARNING: ERROR [Duplicate mapping at virtual address 0x%08x?]\n' % (virt_start))
		return mappings

	def create_flat_mapping(self, pg_table):
		tmp_mapping = {}
		fl_pte = pg_table
		for fl_index in range(0,self.NUM_FL_PTE):
			fl_pg_table_entry = self.ram_dump.read_word(fl_pte)

			if fl_pg_table_entry is not None:
				if fl_pg_table_entry & self.FL_TYPE_SECT:
					(phy_addr, page_size, map_type, status) = self.get_sect_mapping_info(fl_pg_table_entry)
					if status:
						if phy_addr != -1:
							tmp_mapping = self.add_flat_mapping(tmp_mapping, fl_index, 0, phy_addr, map_type, page_size, True)
						else:
							#no mapping
							tmp_mapping = self.add_flat_mapping(tmp_mapping, fl_index, 0, -1, 0, 0, False)
				elif fl_pg_table_entry & self.FL_TYPE_TABLE:
					sl_pte = fl_pg_table_entry & self.FL_BASE_MASK

					for sl_index in range(0,self.NUM_SL_PTE):
						(phy_addr, page_size, map_type, status) = self.get_mapping_info(sl_pte, sl_index)
						if status:
							if phy_addr != -1:
								tmp_mapping = self.add_flat_mapping(tmp_mapping, fl_index, sl_index, phy_addr, map_type, page_size, True)
							else:
								#no mapping
								tmp_mapping = self.add_flat_mapping(tmp_mapping, fl_index, sl_index, -1, 0, 0, False)
						else:
							self.out_file.write('[!] WARNING: FL_PTE[%d] SL_PTE[%d] ERROR [Unknown error]\n' % (fl_index, sl_index))
				elif fl_pg_table_entry != 0:
					self.out_file.write('[!] WARNING: FL_PTE[%d] = %x NOTE: ERROR [Cannot understand first level page table entry]\n' % (fl_index, fl_pg_table_entry))
				else:
					tmp_mapping = self.add_flat_mapping(tmp_mapping, fl_index, 0, -1, 0, 0, False)
			else:
				self.out_file.write('[!] WARNING: FL_PTE[%d] NOTE: ERROR [Cannot understand first level page table entry]\n' % (fl_index))
			fl_pte += 4
		return tmp_mapping

	def create_collapsed_mapping(self, flat_mapping):
		collapsed_mapping = {}
		if len(flat_mapping.keys()) > 0:
			virt_addrs = sorted(flat_mapping.keys())
			start_map = prev_map = flat_mapping[virt_addrs[0]]
			last_mapping = False
			for virt in virt_addrs[1:]:
				map = flat_mapping[virt]
				new_mapping = False
				if map.mapping_size == prev_map.mapping_size and map.mapping_type == prev_map.mapping_type and map.mapped == prev_map.mapped:
					if prev_map.mapping_size == SZ_4K:
						if (map.phys - SZ_4K) != prev_map.phys and map.phys != prev_map.phys:
							new_mapping = True
					elif prev_map.mapping_size == SZ_64K:
						if (map.phys - SZ_64K) != prev_map.phys and map.phys != prev_map.phys:
							new_mapping = True
					elif prev_map.mapping_size == SZ_1M:
						if (map.phys - SZ_1M) != prev_map.phys and map.phys != prev_map.phys:
							new_mapping = True
					elif prev_map.mapping_size == SZ_16M:
						if (map.phys - SZ_16M) != prev_map.phys and map.phys != prev_map.phys:
							new_mapping = True
					elif virt == virt_addrs[-1]:
						#Last one
						last_mapping = True
				else:
					new_mapping = True
				if new_mapping:
					collapsed_mapping = self.add_collapsed_mapping(collapsed_mapping, start_map.virt, map.virt,
																	start_map.phys, prev_map.phys+prev_map.mapping_size,
																	prev_map.mapping_type, prev_map.mapping_size, prev_map.mapped)
					start_map = map
				elif last_mapping:
					collapsed_mapping = self.add_collapsed_mapping(collapsed_mapping, start_map.virt, 0xFFFFFFFF+1,
																	start_map.phys, prev_map.phys+prev_map.mapping_size,
																	prev_map.mapping_type, prev_map.mapping_size, prev_map.mapped)
				prev_map = map
		return collapsed_mapping

	def print_page_table_pretty(self, pg_table):
		flat_mapping = self.create_flat_mapping(pg_table)
		collapsed_mapping = self.create_collapsed_mapping(flat_mapping)

		for virt in sorted(collapsed_mapping.keys()):
			mapping = collapsed_mapping[virt]
			if mapping.mapped:
				self.out_file.write('0x%08x--0x%08x [0x%08x] A:0x%08x--0x%08x [0x%08x] %s[%s]\n' % (mapping.virt_start, mapping.virt_end, mapping.virt_size(),
																						mapping.phys_start, mapping.phys_end,
																						mapping.phys_size(), mapping.mapping_type, MAP_SIZE_STR[get_order(mapping.mapping_size)]))
			else:
				self.out_file.write('0x%08x--0x%08x [0x%08x] [UNMAPPED]\n' % (mapping.virt_start, mapping.virt_end, mapping.virt_size()))

	def iommu_parse(self):
		iommu_domains_rb_root = self.ram_dump.addr_lookup(IOMMU_DOMAIN_VAR)
		if iommu_domains_rb_root is None :
			print_out_str ('[!] WARNING: IOMMU domains was not found in this build. No IOMMU page tables will be generated')
			return

		out_dir = self.ram_dump.outdir

		iommu_domains_rb_root_addr = self.ram_dump.read_word(iommu_domains_rb_root)
		rb_walker = rb_tree.RbTreeWalker(self.ram_dump)
		rb_walker.walk(iommu_domains_rb_root_addr, self.iommu_domain_func)

		for d in self.domain_list:
			self.out_file = open('%s/msm_iommu_domain_%02d.txt' % (out_dir, d.domain_num), 'wb')
			redirect = 'OFF'
			if d.redirect is None:
				redirect = 'UNKNOWN'
			elif d.redirect > 0:
				redirect = 'ON'
			iommu_context = 'None attached'
			if len(d.ctx_list) > 0:
				iommu_context = ''
				for (num, name) in d.ctx_list:
					iommu_context += '%s (%d) ' % (name, num)
			iommu_context = iommu_context.strip()

			self.out_file.write('IOMMU Context: %s. Domain: %s (%d) [L2 cache redirect for page tables is %s]\n' % (iommu_context, d.client_name, d.domain_num, redirect))
			self.out_file.write('[VA Start -- VA End  ] [Size      ] [PA Start   -- PA End  ] [Size      ] [Read/Write][Page Table Entry Size]\n')
			if d.pg_table == 0:
				self.out_file.write('No Page Table Found. (Probably a secure domain)\n')
			else:
				self.print_page_table_pretty(d.pg_table)
				self.out_file.write('\n-------------\nRAW Dump\n')
				self.print_page_table(d.pg_table)
			self.out_file.close()

