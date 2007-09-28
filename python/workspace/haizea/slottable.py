from sets import Set

import workspace.haizea.constants as constants
import workspace.haizea.db as db
from workspace.haizea.log import info, debug, warning

class SlotTable(object):
    def __init__(self, scheduler):
        self.scheduler = scheduler
        self.rm = scheduler.rm
        
        templatedb = self.rm.config.getDBTemplate()
        targetdb = self.rm.config.getTargetDB()
        if targetdb == "memory":
            self.db = db.SQLiteSlotTableDB.toMemFromFile(templatedb)
        else:
            self.db = db.SQLiteSlotTableDB.toFileFromFile(templatedb, targetdb)
            
        self.createDatabase()

        
    def createDatabase(self):
        numnodes = self.rm.config.getNumPhysicalNodes()
        resources = self.rm.config.getResourcesPerPhysNode()
        bandwidth = self.rm.config.getBandwidth()
        
        slottypes = []
        for r in resources:
            resourcename = r.split(",")[0]
            resourcecapacity = r.split(",")[1]
            slottypes.append((self.db.getSlotTypeID(resourcename), resourcecapacity))
            
        # Create nodes
        for node in range(numnodes):
            nod_id = self.db.addNode("fakenode-%i.mcs.anl.gov" % (node+1))
            # Create slots
            for slottype in slottypes:
                self.db.addSlot(nod_id,slt_id=slottype[0],sl_capacity=slottype[1])
                
        # Create image nodes
        nod_id = self.db.addNode("fakeimagenode-exact.mcs.anl.gov")
        sl_id = self.db.addSlot(nod_id,slt_id=constants.RES_NETOUT,sl_capacity=bandwidth)
        
        self.imagenodeslot_exact = sl_id

        nod_id = self.db.addNode("fakeimagenode-besteffort.mcs.anl.gov")
        sl_id = self.db.addSlot(nod_id,slt_id=constants.RES_NETOUT,sl_capacity=bandwidth)
        
        self.imagenodeslot_besteffort = sl_id
        self.db.commit()
        
    def commit(self):
        self.db.commit()
    
    def rollback(self):
        self.db.rollback()
    
    def getNextChangePoint(self, time):
        return self.db.popNextChangePoint(time)
    
    def fitExact(self, leaseID, start, end, vmimage, resreq, prematureend=None, preemptible=False, canpreempt=True):
        numnodes = resreq[constants.RES_CPU]
        slottypes = resreq.keys()
        
        # One CPU per virtual machine
        # TODO: Add a numnodes field to the lease descriptor, and have
        #       the CPU resource refer to the CPU per physical node
        resreq[constants.RES_CPU] = 1
        
        candidatenodes = self.candidateNodesInRange(start,end,resreq,canpreempt)

        # Decide if we can actually fit the entire VW
        # This is the point were we can apply different slot fitting algorithms
        # Currently, a greedy algorithm is used
        # This is O(numvirtualnodes*numphysicalnodes), but could be implemented
        # in O(numvirtualnodes). We'll worry about that later.
        allfits = True
        assignment = {}
        nodeassignment = {}
        orderednodes = self.prioritizenodes(candidatenodes,vmimage,start, resreq, canpreempt)
        
        # We try to fit each virtual node into a physical node
        # First we iterate through the physical nodes trying to fit the virtual node
        # without using preemption. If preemption is allowed, we iterate through the
        # nodes again but try to use preemptible resources.
        
        # This variable keeps track of how many resources we have to preempt on a node
        mustpreempt={}
        info("Node ordering: %s" % orderednodes, constants.ST, self.rm.time)

        for vwnode in range(0,numnodes):
            assignment[vwnode] = {}
            # Without preemption
            for physnode in orderednodes:
                fits = True
                for slottype in slottypes:
                    res = resreq[slottype]
                    if res > candidatenodes[physnode][slottype][2]:
                        fits = False
                if fits:
                    for slottype in slottypes:
                        res = resreq[slottype]
                        assignment[vwnode][slottype] = candidatenodes[physnode][slottype][1]
                        nodeassignment[vwnode] = physnode

                        candidatenodes[physnode][slottype][2] -= res
                        candidatenodes[physnode][slottype][3] -= res
                    break # Ouch
            else:
                if not canpreempt:
                    raise SchedException, "Could not fit node %i in any physical node (w/o preemption)" % vwnode
                # Try preemption
                for physnode in orderednodes:
                    fits = True
                    for slottype in slottypes:
                        res = resreq[slottype]
                        if res > candidatenodes[physnode][slottype][3]:
                            fits = False
                    if fits:
                        for slottype in slottypes:
                            res = resreq[slottype]
                            assignment[vwnode][slottype] = candidatenodes[physnode][slottype][1]
                            nodeassignment[vwnode] = physnode
                            # See how much we have to preempt
                            # Precond: res > candidatenodes[physnode][slottype][2]
                            if candidatenodes[physnode][slottype][2] > 0:
                                res -= candidatenodes[physnode][slottype][2]
                                candidatenodes[physnode][slottype][2] = 0
                            candidatenodes[physnode][slottype][3] -= res
                            if not mustpreempt.has_key(physnode):
                                mustpreempt[physnode] = {}
                            if not mustpreempt[physnode].has_key(slottype):
                                mustpreempt[physnode][slottype]=res                                
                            else:
                                mustpreempt[physnode][slottype]+=res                                
                        break # Ouch
                else:
                    raise SchedException, "Could not fit node %i in any physical node (w/preemption)" % vwnode

        if allfits:
            transfers = []
            info("The VM reservations for this lease are feasible", constants.ST, self.rm.time)
            self.db.addReservation(leaseID, "LEASE #%i" % leaseID)
            for vwnode in range(0,numnodes):
                rsp_name = "VM %i" % (vwnode+1)
                rsp_id = self.db.addReservationPart(leaseID, rsp_name, 1, preemptible)
                transfers.append((nodeassignment[vwnode], rsp_id))
                for slottype in slottypes:
                    amount = resreq[slottype]
                    sl_id = assignment[vwnode][slottype]
                    self.db.addAllocation(rsp_id, sl_id, start, end, amount, realEndTime = prematureend)
            return nodeassignment, mustpreempt, transfers
        else:
            raise SchedException
        
    def candidateNodesInRange(self, start, end, resreq, canpreempt):
        slottypes = resreq.keys()
        
        # This variable stores the candidate slots. It is a dictionary, where the key
        # is the slot type (i.e. this allows us easy access to the candidate slots for
        # one type of slot). Each item is a list containing:
        #    1. Node id
        #    2. Slot id
        #    3. Maximum available resources in that slot (without preemption)
        #    4. Maximum available resources in that slot (with preemption)
        candidateslots = {}
        
        # TODO: There are multiple points at which the slot fitting could be aborted
        # for lack of resources. Currently, this is only checked at the very end.
        for slottype in slottypes:
            candidateslots[slottype] = []
            needed = resreq[slottype]
            
            # This variable stores the (possibly multiple) availabilities of a slot
            # during the requested time interval. It is a dictionary with the slot id
            # as a key. The value is itself a dictionary, with the time (at which
            # availability is measured) as a key. The value is a list with two elements:
            #    [ available resources,
            #      available resources (assuming we can preempt ]
            capacity={}
            
            # This is a list of candidate slot id's
            slots = []

            # TODO: Lots of code here which can be factored out into a function

            # First sieve: select all slots that have enough resources at the beginning
            slotsbegin = []
            slots1 = self.db.findAvailableSlots(time=start, amount=needed, type=slottype, canpreempt=False)
            for slot in slots1:
                slot_id = slot["SL_ID"]
                slotsbegin.append(slot_id)
                if not capacity.has_key(slot_id):
                    capacity[slot_id] = {}
                capacity[slot_id][start] =[slot["available"],0]

            # Find available resources if we can do preemption    
            if canpreempt:
                slots1 = self.db.findAvailableSlots(time=start, amount=needed, type=slottype, canpreempt=True)
                for slot in slots1:
                    slot_id = slot["SL_ID"]
                    if not slot_id in slots:
                        slotsbegin.append(slot_id)
                    if not capacity.has_key(slot_id):
                        capacity[slot_id] = {}
                        capacity[slot_id][start]=[0,slot["available"]]
                    else:
                        capacity[slot_id][start][1]=slot["available"]

            # Second sieve: remove slots that don't have enough resources at the end
            slots2 = self.db.findAvailableSlots(time=end, amount=needed, slots=slotsbegin, closed=False, canpreempt=False)
            slots = []
            for slot in slots2:
                nod_id = slot["NOD_ID"]
                slot_id = slot["SL_ID"]
                slots.append((nod_id,slot_id))
                if not capacity.has_key(slot_id):
                    capacity[slot_id] = {}
                capacity[slot_id][end] = [slot["available"], 0]

            # Find available resources if we can do preemption    
            if canpreempt:
                slots2 = self.db.findAvailableSlots(time=end, amount=needed, slots=slotsbegin, closed=False, canpreempt=True)
                for slot in slots2:
                    nod_id = slot["NOD_ID"]
                    slot_id = slot["SL_ID"]
                    if not (nod_id,slot_id) in slots:
                        slots.append((nod_id,slot_id))
                    if not capacity.has_key(slot_id):
                        capacity[slot_id] = {}
                        capacity[slot_id][end]=[0,slot["available"]]
                    else:
                        if capacity[slot_id].has_key(end):
                            capacity[slot_id][end][1]=slot["available"]
                        else:
                            capacity[slot_id][end] = [0,slot["available"]]

            # Final sieve: Determine "resource change points" and make sure that 
            # there is enough resources at each point too (and determine maximum
            # available)
            cur = self.db.findChangePoints(start, end, closed=False, withSlots=True)
            changepointsslots = cur.fetchall()
            for slot in slots:
                slot_id=slot[1]
                             
                changepoints = [r for r in changepointsslots if r["sl_id"] == slot_id]
                for point in changepoints:
                    time = point["time"]

                    cur = self.resDB.findAvailableSlots(time=time, slots=[slot_id], canpreempt=False)
                    avail = cur.fetchone()["available"]
                    if canpreempt:
                        cur = self.resDB.findAvailableSlots(time=time, slots=[slot_id], canpreempt=True)
                        availpreempt = cur.fetchone()["available"]
                    else:
                        availpreempt = 0
                        
                    capacity[slot_id][time]=[avail, availpreempt]
                    
                maxavail = float("inf")
                if canpreempt:
                    maxavailpreempt = float("inf")
                for cap in capacity[slot_id].values():
                    if cap[0] < maxavail:
                        maxavail = cap[0]
                    if canpreempt and cap[1] < maxavailpreempt:
                        maxavailpreempt = cap[1]
                        
                if not canpreempt and maxavail >= needed:
                    candidateslots[slottype].append([slot[0],slot[1],maxavail, maxavail])
                elif canpreempt and maxavailpreempt >= needed:
                    candidateslots[slottype].append([slot[0],slot[1],maxavail, maxavailpreempt])
            
            info("Slot type %i has candidates %s" % (slottype,candidateslots[slottype]), constants.ST, self.rm.time)
        
        # Make sure that available resources are all available on the same node
        # (e.g. discard candidate nodes where we can provision memory but not the cpu)
        nodeset = Set()
        for slottype in slottypes:
            nodes = Set([s[0] for s in candidateslots[slottype]])
            nodeset |= nodes
            
        for slottype in slottypes:
            nodes = Set([s[0] for s in candidateslots[slottype]])
            nodeset &= nodes
                
        if len(nodeset) == 0:
            raise SchedException, "No physical node has enough available resources for this request"
        
        # This variable contains essentially the same information as candidateslots, except
        # access is done by node id and slottype (i.e. provides an easy way of asking
        # "what is the slot id of the memory slot in node 5")
        # Items have the same information as candidateslots.
        candidatenodes = {}
        
        for slottype in slottypes:
            for slot in candidateslots[slottype]:
                nod_id = slot[0]
                if nod_id in nodeset:
                    if not candidatenodes.has_key(nod_id):
                        candidatenodes[nod_id]={}
                    candidatenodes[nod_id][slottype] = slot
        for node in nodeset:    
            info("Node %i has final candidates %s" % (node,candidatenodes[node]), constants.ST, self.rm.time)
            
        return candidatenodes

    def prioritizenodes(self,candidatenodes,vmimage,start,resreq, canpreempt):
        # TODO2: Choose appropriate prioritizing function based on a
        # config file, instead of hardcoding it)
        #
        # TODO3: Basing decisions only on CPU allocations. This is ok for now,
        # since the memory allocation is proportional to the CPU allocation.
        # Later on we need to come up with some sort of weighed average.
        
        nodes = candidatenodes.keys()
        
        # TODO
        #reusealg = self.config.get(GENERAL_SEC, REUSEALG_OPT)
        nodeswithimg=[]
        #if reusealg=="cache":
        #    nodeswithimg = self.backend.getNodesWithCachedImg(imguri)
        #elif reusealg=="cowpool":
        #    nodeswithimg = self.backend.getNodesWithImgLater(imguri, startTime)
        
        # Compares node x and node y. 
        # Returns "x is ??? than y" (???=BETTER/WORSE/EQUAL)
        def comparenodes(x,y):
            hasimgX = x in nodeswithimg
            hasimgY = y in nodeswithimg

            need = resreq[constants.RES_CPU]
            # First comparison: A node with no preemptible VMs is preferible
            # to one with preemptible VMs (i.e. we want to avoid preempting)
            availX = candidatenodes[x][constants.RES_CPU][2]
            availpX = candidatenodes[x][constants.RES_CPU][3]
            preemptibleres = availpX - availX
            hasPreemptibleX = preemptibleres > 0
            
            availY = candidatenodes[y][constants.RES_CPU][2]
            availpY = candidatenodes[y][constants.RES_CPU][3]
            preemptibleres = availpY - availY
            hasPreemptibleY = preemptibleres > 0

            canfitX = availX / need
            canfitY = availY / need
            
            if hasPreemptibleX and not hasPreemptibleY:
                return constants.WORSE
            elif not hasPreemptibleX and hasPreemptibleY:
                return constants.BETTER
            elif not hasPreemptibleX and not hasPreemptibleY:
                if hasimgX and not hasimgY: 
                    return constants.BETTER
                elif not hasimgX and hasimgY: 
                    return constants.WORSE
                else:
                    if canfitX > canfitY: return constants.BETTER
                    elif canfitX < canfitY: return constants.WORSE
                    else: return constants.EQUAL
            elif hasPreemptibleX and hasPreemptibleY:
                # If both have (some) preemptible resources, we prefer those
                # that involve the less preemptions
                canfitpX = availpX / need
                canfitpY = availpY / need
                preemptX = canfitpX - canfitX
                preemptY = canfitpY - canfitY
                if preemptX < preemptY:
                    return constants.BETTER
                elif preemptX > preemptY:
                    return constants.WORSE
                else:
                    if hasimgX and not hasimgY: return constants.BETTER
                    elif not hasimgX and hasimgY: return constants.WORSE
                    else: return constants.EQUAL
        
        # Order nodes
        nodes.sort(comparenodes)
        return nodes

