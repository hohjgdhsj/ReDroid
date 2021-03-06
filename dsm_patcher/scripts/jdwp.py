import json
import socket
import struct
import time

from threading import Thread, Lock, Event
from Queue import Queue, Empty as EmptyQueue

# python  struct pack format
# c    char string of length 1    1
# B    unsigned char    integer    1
# H    unsigned short    integer    2
# I    unsigned long    integer    4
# Q    unsigned long long    integer 8
# TODO: assemble (un)pack strings with results from id command

class EOF(Exception):
    def __init__(self, inner=None):
        Exception.__init__(
            self, str(inner) if inner else "EOF"
        )

class HandshakeError(Exception):
    def __init__(self):
        Exception.__init__(
            self, 'handshake error, received message did not match'
        )

class ProtocolError(Exception):
    pass


JDWP_HEADER_SIZE = 11
CMD_PKT = '0'
REPLY_PKT = '1'
REPLY_PACKET_TYPE = 0x80
HANDSHAKE_MSG = 'JDWP-Handshake'
JOIN_TIMEOUT = 0.2

EVENT_BREAKPOINT = 2
EVENT_CLASS_PREPARE = 8
EVENT_METHOD_ENTRY = 40
EVENT_METHOD_EXIT_WITH_RETURN_VALUE = 42
EVENTREQUEST_MODKIND_CLASSMATCH = 5

SUSPEND_NONE = 0
SUSPEND_ALL = 2

LEN_METHOD_ENTRY_AND_EXIT_WITH_RETURN_VALUE_HEADER = 43
LEN_CLASS_PREPARE_HEADER = 27

class JDWPConnection(Thread):

    def __init__(self, addr, port, trace=False):
        Thread.__init__(self)

        self.bindqueue = Queue()
        self.reply_pkt_map = {}
        self.cmd_pkt_queue = Queue()

        self.socket_conn = socket.create_connection((addr, port))
        self.next_id = 1

        self.trace = trace

        self.unplug_flag = Event()
        self.lock = Lock()

        self.breakpoint_handler = None
        self.class_prepare_handler = None

    def set_breakpoint_handler(self, handler):
        self.breakpoint_handler = handler

    def set_class_prepare_handler(self, handler):
        self.class_prepare_handler = handler

    def do_read(self, amt):
        """
        Read data from the socket
        """
        req = amt
        buf = ''
        while req:
            pkt = self.socket_conn.recv(req)
            if not pkt: raise EOF()
            buf += pkt
            req -= len(pkt)
        if self.trace:
            print "===> RX:", repr(buf)
        return buf

    def do_write(self, data):
        """
        Write data to the socket
        """
        try:
            if self.trace:
                print "===> TX:", repr(data)
            self.socket_conn.sendall(data)
        except Exception as exc:
            raise EOF(exc)

    def read(self, sz):
        """
        Read data with size sz
        """
        if sz == 0:
            return ''
        pkt = self.do_read(sz)
        if not len(pkt):
            # raise exception if there is nothing to read
            raise EOF()
        return pkt

    def write_id_size(self):
        """
        Send the id size cmd to the VM
        """
        length = JDWP_HEADER_SIZE
        ident = self.acquire_ident()
        flags  = 0
        cmd = 0x0107
        header = struct.pack('>IIBH', length, ident, flags, cmd)
        return self.do_write(header)

    def read_id_size(self):
        """
        Parse the read id size result
        """
        head = self.read_header()
        if head[0] != 20 + JDWP_HEADER_SIZE:
            raise ProtocolError('expected size of an idsize response')
        if head[2] != REPLY_PACKET_TYPE:
            raise ProtocolError('expected first server message to be a response')
        if head[1] != 1:
            raise ProtocolError('expected first server message to be 1')

        body = self.read(20)
        data = struct.unpack(">IIIII", body)
        self.sizes = list(data)
        setattr(self, "fieldIDSize", self.sizes[0])
        setattr(self, "methodIDSize", self.sizes[1])
        setattr(self, "objectIDSize", self.sizes[2])
        setattr(self, "threadIDSize", self.sizes[2])
        setattr(self, "referenceTypeIDSize", self.sizes[3])
        setattr(self, "frameIDSize", self.sizes[4])
        return None

    def read_handshake(self):
        """
        Read the jdwp handshake
        """
        data = self.read(len(HANDSHAKE_MSG))
        if data != HANDSHAKE_MSG:
            raise HandshakeError()

    def write_handshake(self):
        """
        Write the jdwp handshake
        """
        return self.do_write(HANDSHAKE_MSG)

    def read_header(self):
        """
        Read the header
        """
        header = self.read(JDWP_HEADER_SIZE)
        data = struct.unpack(">IIBH", header)
        return data

    def process_data_from_vm(self):
        """
        Handle data from the VM, both the response from VM initated by the
        Debugger and VM's request initated by the VM
        """
        size, ident, flags, code = self.read_header()
        size -= JDWP_HEADER_SIZE
        data = self.read(size)
        try:
            # We process binds after receiving messages to prevent a race
            while True:
                # With False passed to bindqueue.get, it will trigger EmptyQueue exception
                # get pending queue from bindqueue, and ack it by queue.put in process_packet
                self.set_bind(*self.bindqueue.get(False))
        except EmptyQueue:
            pass

        self.process_packet(ident, code, data, flags)

    def set_bind(self, pkt_type, ident, chan):
        """
        Bind the queue for REPLY_PKT
        not for CMD_PKT, they're buffered or handled by handlers
        """
        if pkt_type == REPLY_PKT:
            self.reply_pkt_map[ident] = chan

    def process_packet(self, ident, code, data, flags):
        """
        Handle packets from VM
        """
        # reply pkt shows only once
        if flags == REPLY_PACKET_TYPE:
            chan = self.reply_pkt_map.get(ident)
            if not chan:
                return
            return chan.put((ident, code, data))
        elif not self.unplug_flag.is_set(): # command packets are buffered
            if code == 0x4064:
                event_kind = struct.unpack(">BIB", data[:6])[2]
                if event_kind in [EVENT_METHOD_ENTRY, EVENT_METHOD_EXIT_WITH_RETURN_VALUE]:
                    self.cmd_pkt_queue.put((ident, code, data))
                elif event_kind == EVENT_BREAKPOINT:
                    Thread(target=self.breakpoint_handler, args=[data]).start()
                elif event_kind == EVENT_CLASS_PREPARE:
                    Thread(target=self.class_prepare_handler, args=[data]).start()

    def get_cmd_packets(self):
        ret_list = []
        while True:
            try:
                ret_list.append(self.cmd_pkt_queue.get(False))
            except EmptyQueue:
                break
        return ret_list

    def acquire_ident(self):
        """
        Get a request id
        """
        ident = self.next_id
        self.next_id += 2
        return ident

    def write_request_data(self, ident, flags, code, body):
        """
        Write the request data to jdwp
        """
        size = len(body) + JDWP_HEADER_SIZE
        header = struct.pack(">IIcH", size, ident, flags, code)
        self.do_write(header)
        return self.do_write(body)

    def request(self, code, data='', timeout=None):
        """
        send a request, then waits for a response; returns response
        conn.request returns code and buf
        """
        # create a new queue to get the response of this request
        queue = Queue()
        with self.lock:
            ident = self.acquire_ident()
            self.bindqueue.put((REPLY_PKT, ident, queue))
            self.write_request_data(ident, chr(0x0), code, data)
        try:
            return queue.get(1, timeout)
        except EmptyQueue:
            return None, None, None

    def run(self):
        """
        Thread function for jdwp
        """
        try:
            while True:
                self.process_data_from_vm()
        except EOF:
            print "EOF"

    def start(self):
        """
        Start the jdwp connection
        """
        self.write_handshake()
        self.read_handshake()
        self.write_id_size()
        self.read_id_size()
        self.unplug()
        Thread.start(self)

    def plug(self):
        self.unplug_flag.clear()

    def unplug(self):
        self.unplug_flag.set()

    def stop(self):
        """
        close the JDWP connection
        """
        try:
            self.unplug()
            self.join(timeout=JOIN_TIMEOUT)
            self.socket_conn.shutdown(socket.SHUT_RDWR)
            self.socket_conn.close()
        except Exception, e:
            pass

class JDWPHelper():
    def __init__(self, jdwp_conn):
        self.jdwp_conn = jdwp_conn
        self.jdwp_conn.set_breakpoint_handler(self.breakpoint_handler)
        self.jdwp_conn.set_class_prepare_handler(self.class_prepare_handler)
        self.class_id2name = {}
        self.method_id2name = {}

    def VirtualMachine_ClassesBySignature(self, signature):
        cmd = 0x0102
        signature_utf8 = unicode(signature).encode("utf-8")
        header_data = struct.pack(">I%ds" % len(signature_utf8), len(signature_utf8), signature_utf8)
        return self.jdwp_conn.request(cmd, header_data)

    def VirtualMachine_Suspend(self):
        cmd = 0x0108
        return self.jdwp_conn.request(cmd)

    def VirtualMachine_Resume(self):
        cmd = 0x0109
        return self.jdwp_conn.request(cmd)

    def ReferenceType_Signature(self, ref_id):
        cmd = 0x0201
        header_data = struct.pack(">Q", ref_id)
        return self.jdwp_conn.request(cmd, header_data)

    def ReferenceType_Methods(self, ref_id):
        cmd = 0x0205
        header_data = struct.pack(">Q", ref_id)
        return self.jdwp_conn.request(cmd, header_data)

    def StringReference_Value(self, str_id):
        cmd = 0x0a01
        header_data = struct.pack(">Q", str_id)
        return self.jdwp_conn.request(cmd, header_data)

    def EventRequest_Set_METHOD_ENTRY(self, class_pattern):
        return self.EventRequest_Set_workload_classmatch(class_pattern, EVENT_METHOD_ENTRY, SUSPEND_NONE)

    def EventRequest_Set_METHOD_EXIT_WITH_RETURN_VALUE(self, class_pattern):
        return self.EventRequest_Set_workload_classmatch(class_pattern, EVENT_METHOD_EXIT_WITH_RETURN_VALUE, SUSPEND_NONE)

    def EventRequest_Set_CLASS_PREPARE(self, class_pattern):
        return self.EventRequest_Set_workload_classmatch(class_pattern, EVENT_CLASS_PREPARE, SUSPEND_ALL)

    def EventRequest_Set_workload_classmatch(self, class_pattern, event_kind, suspend_policy):
        cmd = 0x0f01
        modifiers = 1

        class_pattern_utf8 = unicode(class_pattern).encode("utf-8")
        modifier_data = struct.pack(">BI%ds" % len(class_pattern_utf8),
                                    EVENTREQUEST_MODKIND_CLASSMATCH,
                                    len(class_pattern_utf8), class_pattern_utf8)

        header_data = struct.pack(">BBI", event_kind, suspend_policy, modifiers)
        ret = self.jdwp_conn.request(cmd, header_data + modifier_data)
        # return requestID's
        return event_kind, struct.unpack(">I", ret[2])[0],

    def EventRequest_Clear(self, event_kind, request_id):
        cmd = 0x0f02
        header_data = struct.pack(">BI", int(event_kind), int(request_id))
        return self.jdwp_conn.request(cmd, header_data)

    def parse_return_value(self, return_value):
        basic_parser = {
            "Z": lambda x: ("boolean", struct.unpack(">?", x)[0]),
            "B": lambda x: ("byte", chr(struct.unpack(">B", x)[0])),
            "C": lambda x: ("char", x.encode("utf8", "ignore")),
            "S": lambda x: ("short", struct.unpack(">h", x)[0]),
            "I": lambda x: ("int", struct.unpack(">i", x)[0]),
            "J": lambda x: ("long", struct.unpack(">q", x)[0]),
            "F": lambda x: ("float", struct.unpack(">f", x)[0]),
            "D": lambda x: ("double", struct.unpack(">d", x)[0]),

            "[": lambda x: ("array", struct.unpack(">Q", x)[0]),
            "L": lambda x: ("object", struct.unpack(">Q", x)[0]),
            "s": lambda x: ("string", struct.unpack(">Q", x)[0]),
            "t": lambda x: ("thread", struct.unpack(">Q", x)[0]),
            "g": lambda x: ("thread_group", struct.unpack(">Q", x)[0]),
            "l": lambda x: ("class_loader", struct.unpack(">Q", x)[0]),
            "c": lambda x: ("class_object", struct.unpack(">Q", x)[0]),

            "V": lambda x: ("void", None)
        }
        if return_value[0] not in basic_parser:
            return "unknown", return_value
        else:
            ret_type, ret_data = basic_parser[return_value[0]](return_value[1:])
            if ret_type == "string":
                ident, code, str_data = self.StringReference_Value(ret_data)
                if not code:
                    str_len = struct.unpack(">I", str_data[:4])[0]
                    ret_data = struct.unpack(">%ds" % str_len, str_data[4:])[0].decode("utf8", "ignore").encode("utf8")
                else: # string finding error, return null object
                    ret_type = "object"
                    ret_data = 0
            return ret_type, ret_data

    def update_class_method_info_by_class_names(self, class_name_list):
        """
        self.class_id2sig = {
            "classId": "className"
        }
        self.method_id2name = {
            "classId": {
                "methodId": {
                    "name": "methodName"
                    "signature": "methodSignature"
                }
            }
        }
        """
        new_class_id2name = {}
        # get class id by class name
        for class_name in class_name_list:
            class_sig = "L%s;" % class_name.replace(".", "/")
            ident, code, data = self.VirtualMachine_ClassesBySignature(class_sig)

            classes = struct.unpack(">I", data[:4])[0]

            for i in range(classes):
                ref_type_tag = struct.unpack(">B", data[4:5])[0]
                type_id = struct.unpack(">Q", data[5:5 + self.jdwp_conn.referenceTypeIDSize])[0]
                if not type_id in self.class_id2name:
                    new_class_id2name[type_id] = class_name
                    self.class_id2name[type_id] = class_name

        # for each class get its method id and method name
        for class_id, class_name in new_class_id2name.iteritems():
            self.method_id2name[class_id] = {}

            ident, code, data = self.ReferenceType_Methods(class_id)
            declared = struct.unpack(">I", data[:4])[0]
            declared_offset = 4

            for i in range(declared):
                method_id = struct.unpack(">Q", data[declared_offset:declared_offset + self.jdwp_conn.methodIDSize])[0]
                declared_offset += self.jdwp_conn.methodIDSize
                name_len = struct.unpack(">I", data[declared_offset:declared_offset + 4])[0]
                declared_offset += 4
                name = struct.unpack(">%ds" % name_len, data[declared_offset:declared_offset + name_len])[0]
                declared_offset += name_len
                signature_len = struct.unpack(">I", data[declared_offset:declared_offset + 4])[0]
                declared_offset += 4
                signature = struct.unpack(">%ds" % signature_len, data[declared_offset:declared_offset + signature_len])[0]
                # add mod bits as well
                declared_offset += signature_len + 4

                self.method_id2name[class_id][method_id] = {
                    "name": name,
                    "signature": signature
                }

    def parse_cmd_packets(self, cmd_packets):
        ret_list = []
        class_id_set = set()
        for cmd_packet in cmd_packets:
            ident, code, data = cmd_packet
            parsed_header = struct.unpack(">BIBIQBQQQ", data[:LEN_METHOD_ENTRY_AND_EXIT_WITH_RETURN_VALUE_HEADER])

            parsed_packet = {
                #"id": ident,
                #"command": code,
                #"suspendPolicy": parsed_header[0],
                #"events": parsed_header[1],
                "eventKind": parsed_header[2],
                #"requestID": parsed_header[3],
                "thread": parsed_header[4],
                #"typeTag": parsed_header[5],
                "classID": parsed_header[6],
                "methodID": parsed_header[7],
                "methodLocation": parsed_header[8],
            }

            if parsed_packet["eventKind"] == EVENT_METHOD_EXIT_WITH_RETURN_VALUE:
                ret_data = self.parse_return_value(data[LEN_METHOD_ENTRY_AND_EXIT_WITH_RETURN_VALUE_HEADER:])
                parsed_packet["returnType"] = ret_data[0]
                parsed_packet["returnValue"] = ret_data[1]

            ret_list.append(parsed_packet)
            class_id_set.add(parsed_header[6])

        for parsed_packet in ret_list:
            class_id = parsed_packet.pop("classID")
            method_id = parsed_packet.pop("methodID")
            class_name = self.class_id2name[class_id]
            method_info = self.method_id2name[class_id][method_id]
            parsed_packet["classMethodName"] = ".".join([class_name, method_info["name"]])
            parsed_packet["signature"] = method_info["signature"]

        return ret_list

    def breakpoint_handler(self, data):
        pass

    def class_prepare_handler(self, data):
        data_idx = 0
        suspend_policy, events, event_kind, request_id, thread, ref_type_tag, type_id = struct.unpack(">BIBIQBQ", data[data_idx:LEN_CLASS_PREPARE_HEADER])
        data_idx = LEN_CLASS_PREPARE_HEADER
        signature_len = struct.unpack(">I", data[data_idx:data_idx + 4])[0]
        data_idx += 4
        signature = struct.unpack(">%ds" % signature_len, data[data_idx:data_idx + signature_len])[0]
        class_name = signature[len("L"):-len(";")].replace("/", ".")
        self.update_class_method_info_by_class_names([class_name])
        self.VirtualMachine_Resume()
