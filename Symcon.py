# see also https://pypi.org/project/symcon/
# see also reference taken from https://github.com/scadawire/tango-mqtt

import time
from tango import AttrQuality, AttrWriteType, DispLevel, DevState, Attr, CmdArgType, UserDefaultAttrProp
from tango.server import Device, attribute, command, DeviceMeta
from tango.server import class_property, device_property
from tango.server import run
import os
import symcon
import json
import threading
from concurrent.futures import ThreadPoolExecutor
import datetime

class Symcon(Device, metaclass=DeviceMeta):
    pass

    host = device_property(dtype=str, default_value="127.0.0.1")
    port = device_property(dtype=int, default_value=1883)
    username = device_property(dtype=str, default_value="")
    password = device_property(dtype=str, default_value="")
    protocol = device_property(dtype=str, default_value="http")
    objectid = device_property(dtype=int, default_value=0)
    updateIntervalPoll = device_property(dtype=int, default_value=5)

    @attribute(dtype=str)
    def time(self):
        return str(datetime.datetime.now())

    def read_dynamic_attr(self, attr):
        name = attr.get_name()
        value = self.dynamicAttributes[name]
        id = self.dynamicAttributeNameIds[name]
        self.debug_stream("read value %s / %s: %s", name, id, value)
        value = self.stringValueToTypeValue(name, value)
        attr.set_value(value)
        return attr

    def _poll_loop(self):
        """Single long-lived background thread — polls all variables on a fixed interval."""
        while not self._stop_event.is_set():
            try:
                self._update_cache()
            except Exception as e:
                self.warn_stream("poll error: %s", str(e))
            self._stop_event.wait(timeout=self.updateIntervalPoll)

    def _update_cache(self):
        """Fetch all variable values concurrently (I/O-bound)."""
        self.debug_stream("starting update of all values")
        start_update = time.time()
        names = list(self.dynamicAttributes.keys())
        with ThreadPoolExecutor(max_workers=min(len(names), 10)) as ex:
            futures = {ex.submit(self.updateValueSingle, n): n for n in names}
            for f in futures:
                try:
                    f.result()
                except Exception as e:
                    self.warn_stream("update issue: %s", str(e))
        self.debug_stream("finished update of all values, took: %ss", round(time.time() - start_update, 2))

    def updateValueSingle(self, name):
        value = str(self.connection.getValue(self.dynamicAttributeNameIds[name], False))
        self.processUpdate(name, value)

    def processUpdate(self, name, value):
        if(self.dynamicAttributes[name] != value):
            id = self.dynamicAttributeNameIds[name]
            self.debug_stream("value %s / %s changed from %s to %s", name, id, self.dynamicAttributes[name], value)
            self.dynamicAttributes[name] = value
            try:
                self.push_change_event(name, self.stringValueToTypeValue(name, value))
            except Exception as e:
                self.warn_stream("update issue: %s", str(e))

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
        self.debug_stream("Publish variable %s / %s: %s", topic, id, value)
        value = self.stringValueToTypeValue(topic, value)
        self.connection.requestAction(id, value)

    @command(dtype_in=str)
    def add_dynamic_attribute(self, valueDetails):
        name = str(valueDetails["ObjectName"])
        id = valueDetails["ObjectID"]
        # tangoName = "symcon-" + str(id) # would be better but issues with current ia references
        tangoName = name
        self.debug_stream("adding dynamic attribute, # %s / name: %s", id, name)
        varDetails = self.getVarDetails(id)
        self.debug_stream("adding dynamic attribute, var details var type: %s", varDetails["VariableType"])
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
        self.debug_stream("adding dynamic attribute, internal var type: %s", variableType)
        self.dynamicAttributeValueTypes[tangoName] = variableType
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

        self.debug_stream("adding dynamic attribute, min_value: %s", min_value)
        self.debug_stream("adding dynamic attribute, max_value: %s", max_value)
        writeType = self.stringValueToWriteType("READ_WRITE") # TODO: is this exposed over symcon?
        self.debug_stream("adding dynamic attribute, writeType: %s", writeType)
        attr = Attr(tangoName, variableType, writeType)
        prop = UserDefaultAttrProp()
        if(min_value != "" and min_value != max_value):
            prop.set_min_value(min_value)
        if(max_value != "" and min_value != max_value):
            prop.set_max_value(max_value)
        if(unit != ""):
            prop.set_unit(unit)
        prop.set_label(name)
        self.debug_stream("adding dynamic attribute, unit: %s", unit)
        attr.set_default_properties(prop)
        self.add_attribute(attr, r_meth=self.read_dynamic_attr, w_meth=self.write_dynamic_attr)
        self.dynamicAttributes[tangoName] = "NEW"
        self.dynamicAttributeNameIds[tangoName] = id
        self.updateValueSingle(tangoName)
        self.info_stream("added attribute name: %s / tango name %s / type: %s / min: %s / max: %s / unit: %s",
            name, tangoName, variableType, min_value, max_value, unit)
        # self.publish([name, self.dynamicAttributes[name]])

    def init_device(self):
        self.set_state(DevState.INIT)
        self.get_device_properties(self.get_device_class())

        # instance-level state (avoids sharing across re-inits or multiple instances)
        self.connection = 0
        self.dynamicAttributes = {}
        self.dynamicAttributeNameIds = {}
        self.dynamicAttributeNameTypes = {}
        self.dynamicAttributeValueTypes = {}
        self._stop_event = threading.Event()

        self.info_stream("Connecting to %s:%s", self.host, self.port)
        self.connection = symcon.Symcon(str(self.host),int(self.port),str(self.protocol),str(self.username),str(self.password))
        self.info_stream("symcon dir: %s", self.connection.execCommand("IPS_GetKernelDir"))
        kernelVersion = self.connection.execCommand("IPS_GetKernelVersion")
        self.info_stream("kernel version: %s", kernelVersion)
        if(float(kernelVersion) < 6):
            raise Exception("Kernel version unsupported, requires 6 and up, detected: " + kernelVersion)

        details = json.loads(self.connection.getObjDetails(self.objectid))
        self.debug_stream("object details: %s", details)
        for valueOrObjectId in details["ChildrenIDs"]:
            self.addValueOrObject("", valueOrObjectId)

        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._poll_thread.start()
        self.set_state(DevState.ON)

    def delete_device(self):
        if hasattr(self, '_stop_event'):
            self._stop_event.set()

    def addValueOrObject(self, prefix, symconId):
        try:
            objDetails = json.loads(self.connection.getObjDetails(symconId))
        except Exception as e:
            self.warn_stream("cannot get object details: %s", str(e))
            return
        objDetails["ObjectName"] = prefix + "_" + objDetails["ObjectName"]
        self.debug_stream("processing object or value: %s | %s", symconId, objDetails["ObjectName"])
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
    print(f"[{time.strftime('%H:%M:%S')}] calling run()")
    run({deviceServerName: Symcon})
