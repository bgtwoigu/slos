#include <page_table.h>
#include <frame_pool.h>
#include <mem_layout.h>

#define ENABLE_MMU		0x00000001
#define ENABLE_DCACHE		0x00000004
#define ENABLE_ICACHE		0x00001000
#define MASK_MMU		0x00000001
#define MASK_DCACHE		0x00000004
#define MASK_ICACHE		0x00001000

static struct pagetable *pcurrentpgt;

void init_pageregion(struct pagetable *ppagetable,
		     struct framepool *pkernel_framepool,
		     struct framepool *pprocess_framepool,
		     const unsigned int _shared_size)
{
	ppagetable->paging_enabled = 0;
	ppagetable->kernel_framepool = pkernel_framepool;
	ppagetable->process_framepool = pprocess_framepool;
	ppagetable->shared_size = _shared_size;
	ppagetable->k_page_dir = 0;
	ppagetable->VMcnt = 0;
}

/* small page translation is used */
void init_pagetable(struct pagetable *ppagetable, PG_TYPE pagetype) 
{
	int i, j;
	int rd;
	
	/* all kernel tasks share the same page directory */
	if (pagetype == PG_TABLE_KERN && ppagetable->k_page_dir)
		return;

	/* page directory is located in process mem pool 
	 * 16KB for 4096 entry(16KB = 4K * 4Byte) is needed
	 */
	if (pagetype == PG_TABLE_USER) {
		ppagetable->page_directory = (unsigned int *)FRAMETOPHYADDR(get_frame(ppagetable->process_framepool));
		/* alloc 3 more contiguous page frames */
		for(i=0 ; i<3 ; i++) {
			FRAMETOPHYADDR(get_frame(ppagetable->process_framepool));
		}
	} else {
		/* alloc 3 more contiguous page frames for page_directory : 4K entry */
		ppagetable->page_directory = (unsigned int *)PGD_START_BASE;
	}

	/* initialize page directory entry as 0 */
	for (i=0 ; i<4095 ; i++) {
		ppagetable->page_directory[i] = 0;
	}

	if(ppagetable->k_page_table == 0) {
		/* 16MB(=4MB *4) directly mapped memory
		 * 0 ~ 4MB : kernel text, data, stack + 
		 * usr task text, data, stack + 
		 * ramdisk img
		 * 4MB ~ 16MB : kernel heap
		 */
		/* to address 16MB kernel area(code+heap), 
		 * 4 page frames are needed 
		 */
		for(i=0 ; i<4 ; i++) {
			ppagetable->k_page_table = (unsigned int *)PGT_START_BASE;
			for(j=0 ; j<4 ; j++) {

				/* 0x11 is
				   Bit[1:0] = 01 : 00:fualt, 01:page, 10:section, 11 : reserved
				   Bit[3:2] = 00 : section : B(ufferable), C(achable), page : don't care
				   Bit[4] = 1
				   Bit[8:5] = 0000 : Domain 0
				   Bit[9] = 0 : don't care
				 */
				ppagetable->page_directory[i*4+j] = ((unsigned int)(ppagetable->k_page_table)+((256*j)<<2) | 0x11);
			}
		}
	}

	/* 0x002
	 * Bit[1:0] = 10 : small page 
	 * Bit[2] = 0 : Not Bufferable
	 * Bit[3] = 0 : Not Cacheable
	 * Bit[5:4] = Bit[7:6] = Bit[9:8] = Bit[11:10] = 00 
	 * ; the 4KB small page is generated by setting all of 
	 * ; the AP bit pairs to the same values, AP3=AP2=AP1=AP0
	 */

	/* initialize 16MB(4 * 4KB page * 1024 entry) direct mapped memory */
	for(i=0 ; i<4 ; i++) {
		ppagetable->k_page_table = (unsigned int *)(ppagetable->page_directory[i*4] & 0xfffffc00);
		for(j=0 ; j<1024 ; j++) {
			ppagetable->k_page_table[i*1024 + j] = (i*(0x1<<22) + j*(0x1<<12)) | 0x002;
		}
	}

	if(pagetype == PG_TABLE_KERN) {
		ppagetable->k_page_dir = ppagetable->page_directory;
	}

	/* set the domain access register as manager mode
	 *  (access permission not checked) 
	 */
	rd = 0xffffffff; 
	asm ("mcr p15, 0, %0, c3, c0, 0" : : "r" (rd) : );
}

void load_pagetable(struct pagetable *ppagetable)
{
	int r0 = 0;

	pcurrentpgt = ppagetable;

	/* write the translation table base 
	 * currently, the userspace app and kernel uses the same memory space.
	 * TODO : need to split kernel space and user space 
	 */
	/* set TTBCR as 0 for enabling using TTBR0.
	 * Let TTBCR = N>0, if all va[31:32-N] == 0, then TTBR0 is used
	 * otherwise TTBR1 is used, For example Linux N=2, va[00xxxx...b] is userspace,
	 * va[0100...b] ~ va[11....b] is for kernel space.
	 * slos sets the TTBCR as 0 and use TTBR0
	 * PD1(c2[5]) is 0 for enble page table walk in TTB1
	 * PD0(c2[4]) is 0 for enable page table walk in TTB0
	 */
	asm ("mcr p15, 0, %0, c2, c0, 2" : : "r" (r0) :);
	/* set TTBR0 as 0 for depricated user space */
	asm ("mcr p15, 0, %0, c2, c0, 0" : : "r" (ppagetable->page_directory) :);
	/* set TTBR1 as kernel page directory base address */
	asm ("mcr p15, 0, %0, c2, c0, 1" : : "r" (ppagetable->page_directory) :);
	/* flush TLB */
	/*asm ("mcr p15, 0, %0, c8, c7, 0" : : "r" (r0) :);*/

	/* invalidate i cache */
	/*asm ("mcr p15, 0, %0, c7, c5, 1" : : "r"(r0) :);*/
	/* invalidate d cache */
	/*asm ("mcr p15, 0, %0, c7, c6, 1" : : "r"(r0) :);*/
}

void enable_paging()
{
	int i;
	unsigned int enable = 0; //ENABLE_MMU ;//| ENABLE_DCACHE | ENABLE_ICACHE;
	unsigned int mask = MASK_MMU | MASK_DCACHE | MASK_ICACHE;
	unsigned int c1;
	int r0 = 0;

	/* read control (c1) register of cp 15*/
	asm ("mrc p15, 0, %0, c1, c0, 0" : "=r" (c1) ::);

	/* disable i/d cache */
	/*asm ("mcr p15, 0, %0, c1, c0, 0" : : "r"(c1) :);*/
	/* invalidate entire i cache */
	/*asm ("mcr p15, 0, %0, c7, c5, 0" : : "r"(r0) :);*/
	/* invalidate entire d cache */
	/*asm ("mcr p15, 0, %0, c7, c6, 0" : : "r"(r0) :);*/

	c1 &= ~mask;
	enable = ENABLE_MMU; // | ENABLE_DCACHE | ENABLE_ICACHE;
	c1 |= enable;
	/* write control register to enable MMU I/D cache */
	asm("mcr p15, 0, %0, c1, c0, 0" : : "r" (c1):);

	/* for test */
	/*i = 1;*/
	/*while(i == 1) ;*/
	/*i = 0;*/
}
#if 0
void EnableMMU (void)
{
	static volatile __attribute__ ((aligned (0x4000))) unsigned PageTable[4096];

	unsigned base;
	for (base = 0; base < 512; base++)
	{
		// outer and inner write back, write allocate, shareable
		PageTable[base] = base << 20 | 0x1140E;
	}
	for (; base < 4096; base++)
	{
		// shared device, never execute
		PageTable[base] = base << 20 | 0x10416;
	}

	// restrict cache size to 16K (no page coloring)
	unsigned auxctrl;
	asm volatile ("mrc p15, 0, %0, c1, c0,  1" : "=r" (auxctrl));
	auxctrl |= 1 << 6;
	asm volatile ("mcr p15, 0, %0, c1, c0,  1" :: "r" (auxctrl));

	// set domain 0 to client
	asm volatile ("mcr p15, 0, %0, c3, c0, 0" :: "r" (1));

	// always use TTBR0
	asm volatile ("mcr p15, 0, %0, c2, c0, 2" :: "r" (0));

	// set TTBR0 (page table walk inner cacheable, outer non-cacheable, shareable memory)
	asm volatile ("mcr p15, 0, %0, c2, c0, 0" :: "r" (3 | (unsigned) &PageTable));

	// invalidate data cache and flush prefetch buffer
	asm volatile ("mcr p15, 0, %0, c7, c5,  4" :: "r" (0) : "memory");
	asm volatile ("mcr p15, 0, %0, c7, c6,  0" :: "r" (0) : "memory");

	// enable MMU, L1 cache and instruction cache, L2 cache, write buffer,
	//   branch prediction and extended page table on
	unsigned mode;
	asm volatile ("mrc p15,0,%0,c1,c0,0" : "=r" (mode));
	mode |= 0x0480180D;
	asm volatile ("mcr p15,0,%0,c1,c0,0" :: "r" (mode) : "memory");
}
#endif

void handle_fault()
{
	int i;
	unsigned int *pda, *pta;
	unsigned int *pfa;
	unsigned int *pde, *pte;
	unsigned int *page_table, *frame_addr;

	/* read DFAR */
	asm volatile ("mrc p15, 0, %0, c6, c0, 0" : "=r" (pfa) ::);
	/* read ttb */
	asm volatile ("mrc p15, 0, %0, c2, c0, 0" : "=r" (pda) ::);
#if 0
	/* check fault address is in valid VM region */
	for(i=0 ; i<VMcnt ; i++) {
		if(pVMref[i]->is_legitimate((unsigned int)pfa)) break;
	}

	/* the fault address is not in valid VM region */
	if(VMcnt != 0 && i == VMcnt) {
		return;
	}
#endif
	/* entry for 1st level descriptor */
	pde = (unsigned int *)((0xffffc000 & *pda) | ((0xfff00000 & *pfa)>>18));

	if(*pde == 0x0) {
		page_table = (unsigned int *)FRAMETOPHYADDR(get_frame(pcurrentpgt->process_framepool));
		for(i=0 ; i<4 ; i++) {
			/* set the value of 1st level descriptor */
			pde[i] = (unsigned int)((unsigned int)page_table + (256*i)<<2 | 0x11); 
		}
	}

	frame_addr = (unsigned int *)FRAMETOPHYADDR(get_frame(pcurrentpgt->process_framepool));

	/* entry for 2nd level descriptor */
	pte = (unsigned int *)((*pde & 0xfffffc00) | ((0x000ff000 & *pfa)>>10));
	/* set the value of 2nd level descriptor */
	*pte = ((unsigned int)frame_addr | 0x55E);
}

#if 0
void PageTable::register_vmpool(VMPool *_pool)
{
	pVMref[VMcnt++] = _pool;
}
#endif

void free_page(unsigned int pageAddr)
{
	unsigned int *pda, *pde, *pte;
	unsigned int *frame_addr;
	unsigned int frame_num, frame_num_k_heap;
	/* read ttb */
	asm volatile ("mrc p15, 0, %0, c2, c0, 0" : "=r" (pda) ::);
	/* get the 1st level descriptor */
	pde = (unsigned int *)((*pda & 0xfffc0000) | ((pageAddr & 0xfff00000)>>18));
	pte = (unsigned int *)((*pde & 0xfffffc00) | ((pageAddr & 0x000ff000)>>10));

	/* physical address of frame */
	frame_addr = (unsigned int *)(*pte);
	frame_num = (unsigned int)(frame_addr) >> 12;

	if(frame_num >= KERNEL_HEAP_START_FRAME &&
 	   frame_num < KERNEL_HEAP_START_FRAME + KERNEL_HEAP_FRAME_NUM) {
		release_frame(pcurrentpgt->kernel_framepool, frame_num);
	} else if(frame_num >= PROCESS_HEAP_START_FRAME &&
		  frame_num < PROCESS_HEAP_START_FRAME + PROCESS_HEAP_FRAME_NUM) {
		release_frame(pcurrentpgt->process_framepool, frame_num);
	} else {
		/* error !
		 * only frames in heap can be freed.
		 */
	}

	*pte = 0x0;
}
