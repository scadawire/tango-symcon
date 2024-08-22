# see also https://pypi.org/project/symcon/
# see also reference taken from https://github.com/scadawire/tango-mqtt
# TODO: other syn attr properties
# TODO: update behaviour

import time
from tango import AttrQuality, AttrWriteType, DispLevel, DevState, Attr, CmdArgType, UserDefaultAttrProp
from tango.server import Device, attribute, command, DeviceMeta
from tango.server import class_property, device_property
from tango.server import run
import os
import symcon
import json
from threading import Thread

class Symcon(Device, metaclass=DeviceMeta):
    pass

    host = device_property(dtype=str, default_value="127.0.0.1")
    port = device_property(dtype=int, default_value=1883)
    username = device_property(dtype=str, default_value="")
    password = device_property(dtype=str, default_value="")
    protocol = device_property(dtype=str, default_value="http")
    objectid = device_property(dtype=int, default_value=0)
    updateIntervalPoll = device_property(dtype=int, default_value=5)
    connection = 0
    dynamicAttributes = {}
    dynamicAttributeNameIds = {}
    dynamicAttributeNameTypes = {}
    dynamicAttributeValueTypes = {}
    last_update = 0
    syncing = False

    @attribute
    def time(self):
        return time.time()

    def read_dynamic_attr(self, attr):
        name = attr.get_name()
        Thread(target=self.updateCache).start()
        value = self.dynamicAttributes[name]
        self.debug_stream("read value " + str(name) + ": " + value)
        value = self.stringValueToTypeValue(name, value)
        attr.set_value(value)
        return attr
    
    def updateCache(self):
        requiresUpdate = (self.last_update == 0 or (self.last_update - time.time()) > self.updateIntervalPoll) and self.syncing == False
        if(requiresUpdate == False): return
        self.syncing = True
        # would be nice to have, but not exposed over symcon: retrieving muitlple variable values at once
        #params = []
        #for n in self.dynamicAttributes:
        #    params.append(self.dynamicAttributeNameIds[n])
        #out = self.connection.send({"method": "GetValue", "params": params, "jsonrpc": "2.0", "id": 0})
        #print(out)

        # trivial implementation, requires one api call per each var
        self.debug_stream("starting update of all values")
        start_update = time.time()
        for n in self.dynamicAttributes:
            try:
                self.updateValue(n)
            except Exception as e:
                self.warn_stream("update issue: " . str(e))
        self.debug_stream("finished update of all values, took: " + str(time.time() - start_update) + "s")
        self.last_update = time.time()
        self.syncing = False

    def updateValue(self, name):
        value = str(self.connection.getValue(self.dynamicAttributeNameIds[name], False))
        self.dynamicAttributes[name] = value

    def write_dynamic_attr(self, attr):
        name = attr.get_name()
        self.dynamicAttributes[name] = str(attr.get_write_value())
        self.publish([name, self.dynamicAttributes[name]])
        self.push_change_event(name)

    def stringValueToTypeValue(self, name, val):
        if(self.dynamicAttributeValueTypes[name] == CmdArgType.DevBoolean):
            if(str(val).lower() == "false"):
                return False
            if(str(val).lower() == "true"):
                return True
            return bool(int(float(val)))
        if(self.dynamicAttributeValueTypes[name] == CmdArgType.DevLong):
            return int(float(val))
        if(self.dynamicAttributeValueTypes[name] == CmdArgType.DevDouble):
            return float(val)
        if(self.dynamicAttributeValueTypes[name] == CmdArgType.DevFloat):
            return float(val)
        return val

    def stringValueToWriteType(self, write_type_name) -> AttrWriteType:
        return AttrWriteType.READ_WRITE
        if(write_type_name == "READ"):
            return AttrWriteType.READ
        if(write_type_name == "WRITE"):
            return AttrWriteType.WRITE
        if(write_type_name == "READ_WRITE"):
            return AttrWriteType.READ_WRITE
        if(write_type_name == "READ_WITH_WRITE"):
            return AttrWriteType.READ_WITH_WRITE
        if(write_type_name == ""):
            return AttrWriteType.READ_WRITE
        raise Exception("given write_type '" + write_type_name + "' unsupported, supported are: READ, WRITE, READ_WRITE, READ_WITH_WRITE")

    @command(dtype_in=[str])
    def publish(self, args):
        topic, value = args
        id = self.dynamicAttributeNameIds[topic]
        tag = "Publish variable " + str(topic) + "/" + str(id) + ": " + str(value)
        self.info_stream(tag)
        value = self.stringValueToTypeValue(topic, value)
        self.connection.requestAction(id, value)

    @command(dtype_in=str)
    def add_dynamic_attribute(self, valueDetails):
        name = valueDetails["ObjectName"]
        id = valueDetails["ObjectID"]
        varDetails = self.getVarDetails(id)
        # see https://www.symcon.de/de/service/dokumentation/befehlsreferenz/variablenverwaltung/ips-getvariable/
        # VariableType (ab 4.0) integer Enthält den Variablentyp (0: Boolean, 1: Integer, 2: Float, 3: String)
        variableType = CmdArgType.DevString
        if(varDetails["VariableType"] == 0):
            variableType = CmdArgType.DevBoolean
        if(varDetails["VariableType"] == 1):
            variableType = CmdArgType.DevLong
        if(varDetails["VariableType"] == 2):
            variableType = CmdArgType.DevDouble
        if(varDetails["VariableType"] == 3):
            variableType = CmdArgType.DevString
        self.dynamicAttributeValueTypes[name] = variableType
        min_value = ""
        max_value = ""
        unit = ""
        if(varDetails["VariableProfile"] != ""):
            unit = str(varDetails["Profile"]["Suffix"])
            if(variableType == CmdArgType.DevDouble or variableType == CmdArgType.DevLong):
                min_value = str(varDetails["Profile"]["MinValue"])
                max_value = str(varDetails["Profile"]["MaxValue"])
                if(variableType == CmdArgType.DevLong): # requires for ints the value to be in int format as well
                    min_value = str(int(float(varDetails["Profile"]["MinValue"])))
                    max_value = str(int(float(varDetails["Profile"]["MinValue"])))

        writeType = self.stringValueToWriteType("READ_WRITE") # TODO: is this exposed over symcon?
        attr = Attr(name, variableType, writeType)
        prop = UserDefaultAttrProp()
        if(min_value != "" and min_value != max_value): 
            prop.set_min_value(min_value)
        if(max_value != "" and min_value != max_value): 
            prop.set_max_value(max_value)
        if(unit != ""): 
            prop.set_unit(unit)
        attr.set_default_properties(prop)
        self.add_attribute(attr, r_meth=self.read_dynamic_attr, w_meth=self.write_dynamic_attr)
        self.dynamicAttributes[name] = str(self.connection.getValue(id, False))
        self.dynamicAttributeNameIds[name] = id
        print("added attribute: " + str(name) + " / type: " + str(variableType)
               + " / min: " + str(min_value) + " / max: " + str(max_value) + " / unit: " + str(unit))
        # self.publish([name, self.dynamicAttributes[name]])

    def init_device(self):
        self.set_state(DevState.INIT)
        self.get_device_properties(self.get_device_class())
        self.info_stream("Connecting to " + str(self.host) + ":" + str(self.port))
        self.connection = symcon.Symcon(str(self.host),int(self.port),str(self.protocol),str(self.username),str(self.password))
        print("symcon dir: " + self.connection.execCommand("IPS_GetKernelDir"))
        kernelVersion = self.connection.execCommand("IPS_GetKernelVersion")
        print("kernel version: " + kernelVersion)
        if(float(kernelVersion) < 6):
            raise Exception("Kernel version unsupported, requires 6 and up, detected: " + kernelVersion)
        
        details = json.loads(self.connection.getObjDetails(self.objectid))
        print("details")
        print(details)
        for valueOrObjectId in details["ChildrenIDs"]:
            self.addValueOrObject("", valueOrObjectId)
        self.set_state(DevState.ON)
        
    def addValueOrObject(self, prefix, symconId):
        objDetails = json.loads(self.connection.getObjDetails(symconId))
        objDetails["ObjectName"] = prefix + "_" + objDetails["ObjectName"]
        print("processing object or value: " + str(symconId) + " | " + objDetails["ObjectName"])
        # siehe auch https://www.symcon.de/de/service/dokumentation/befehlsreferenz/objektverwaltung/ips-getobject/
        if objDetails["ObjectType"] == 6: 
            self.addValueOrObject(prefix, self.resolveObjectLink(symconId))
        # siehe auch https://www.symcon.de/de/service/dokumentation/befehlsreferenz/objektverwaltung/ips-getobject/
        elif objDetails["ObjectType"] == 2:
            self.add_dynamic_attribute(objDetails)
        else:
            for valueOrObjectId in objDetails["ChildrenIDs"]:
                self.addValueOrObject(objDetails["ObjectName"], valueOrObjectId)
    
    def getVarDetails(self, varId):
        out = self.connection.send({"method": "IPS_GetVariable", "params": [varId], "jsonrpc": "2.0", "id": 0})
        if(out["VariableProfile"] != ""):
            out["Profile"] = self.connection.send({"method": "IPS_GetVariableProfile", "params": [out["VariableProfile"]], "jsonrpc": "2.0", "id": 0})
        return out
    
    def resolveObjectLink(self, linkId):
        # see also https://www.symcon.de/de/service/dokumentation/befehlsreferenz/linkverwaltung/ips-getlink/
        resolve = self.connection.send({"method": "IPS_GetLink", "params": [linkId], "jsonrpc": "2.0", "id": 0})
        return resolve["TargetID"]
    
if __name__ == "__main__":
    deviceServerName = os.getenv("DEVICE_SERVER_NAME")
    run({deviceServerName: Symcon})