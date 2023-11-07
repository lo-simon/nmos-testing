# Copyright (C) 2023 Advanced Media Workflow Association
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from calendar import c

import json
import os
import random
import re
import select
import string
import sys
import textwrap
import time

from itertools import product
from jsonschema import ValidationError, SchemaError
from math import floor
from xeger import Xeger

from ..Config import WS_MESSAGE_TIMEOUT, IS12_INTERATIVE_TESTING
from ..GenericTest import NMOSTestException, NMOSTestException
from ..ControllerTest import ControllerTest, TestingFacadeException
from ..IS12Utils import IS12Utils, NcObject, NcMethodStatus, NcBlockProperties,  NcPropertyChangeType, \
    NcObjectMethods, NcObjectProperties, NcObjectEvents, NcClassManagerProperties, NcDeviceManagerProperties, \
    StandardClassIds, NcClassManager, NcBlock
from ..TestHelper import load_resolved_schema
from ..TestResult import Test

NODE_API_KEY = "node"
CONTROL_API_KEY = "ncp"
MS05_API_KEY = "controlframework"
FEATURE_SETS_KEY = "featuresets"


class IS1202Test(ControllerTest):

    def __init__(self, apis, **kwargs):
        ControllerTest.__init__(self, apis, **kwargs)
        self.node_url = self.apis[NODE_API_KEY]["url"]
        self.ncp_url = self.apis[CONTROL_API_KEY]["url"]
        self.is12_utils = IS12Utils(self.node_url,
                                    self.apis[CONTROL_API_KEY]["spec_path"],
                                    self.apis[CONTROL_API_KEY]["spec_branch"])
        self.load_reference_resources()
        self.device_model = None
        self.constraint_error = False
        self.constraint_error_msg = ""
        self.device_model_metadata = {"checked": False, "error": False, "error_msg": ""}

    def set_up_tests(self):
        # Don't set up mock resources as not needed
        pass

    def tear_down_tests(self):
        # Clean up Websocket resources
        self.is12_utils.close_ncp_websocket()

    def pre_tests_message(self):
        """
        Introduction to IS-12 Invasive Tests
        """
        if not IS12_INTERATIVE_TESTING:
            return

        # In order to give the tests some context, a pre tests message is displayed
        # on the Testing Fa�ade prior to the tests starting. This communicates any
        # pre-requisites or setup required by the Test User. 
        question = textwrap.dedent(f"""\
                   These tests validate a Node under test's MS-05 Device Model using IS-12.

                   These tests are invasive and could cause harm to the Node under test.
                   
                   !!!Care should therefore be taken when running these tests!!!
                                   
                   Each test will allow parts of the Device Model to be exluded from the testing.

                   Start the tests by clicking the 'Next' button.
                   """)

        try:
            self._invoke_testing_facade(question, [], test_type="action")

        except TestingFacadeException:
            # pre_test_introducton timed out
            pass

    def post_tests_message(self):
        """
        IS-12 Test Suite complete!
        """
        if not IS12_INTERATIVE_TESTING:
            return
        # Once the tests are complete this post tests message is displayed.

        question = """\
                   IS-12 tests complete!

                   Please press the 'Next' button to exit the tests.
                   """

        try:
            self._invoke_testing_facade(question, [], test_type="action")

        except TestingFacadeException:
            # post_test_introducton timed out
            pass
    def load_model_descriptors(self, descriptor_paths):
        descriptors = {}
        for descriptor_path in descriptor_paths:
            for filename in os.listdir(descriptor_path):
                name, extension = os.path.splitext(filename)
                if extension == ".json":
                    with open(os.path.join(descriptor_path, filename), 'r') as json_file:
                        descriptors[name] = json.load(json_file)

        return descriptors

    def generate_json_schemas(self, datatype_descriptors, schema_path):
        """Generate datatype schemas from datatype descriptors"""
        datatype_schema_names = []
        base_schema_path = os.path.abspath(schema_path)
        if not os.path.exists(base_schema_path):
            os.makedirs(base_schema_path)

        for name, descriptor in datatype_descriptors.items():
            json_schema = self.is12_utils.descriptor_to_schema(descriptor)
            with open(os.path.join(base_schema_path, name + '.json'), 'w') as output_file:
                json.dump(json_schema, output_file, indent=4)
                datatype_schema_names.append(name)

        # Load resolved MS-05 datatype schemas
        datatype_schemas = {}
        for name in datatype_schema_names:
            datatype_schemas[name] = load_resolved_schema(schema_path, name + '.json', path_prefix=False)

        return datatype_schemas

    def load_reference_resources(self):
        """Load datatype and control class decriptors and create datatype JSON schemas"""
        # Calculate paths to MS-05 descriptors
        # including Feature Sets specified as additional_paths in test definition
        spec_paths = [os.path.join(self.apis[FEATURE_SETS_KEY]["spec_path"], path)
                      for path in self.apis[FEATURE_SETS_KEY]["repo_paths"]]
        spec_paths.append(self.apis[MS05_API_KEY]["spec_path"])

        datatype_paths = []
        classes_paths = []
        for spec_path in spec_paths:
            datatype_path = os.path.abspath(os.path.join(spec_path, 'models/datatypes/'))
            if os.path.exists(datatype_path):
                datatype_paths.append(datatype_path)
            classes_path = os.path.abspath(os.path.join(spec_path, 'models/classes/'))
            if os.path.exists(classes_path):
                classes_paths.append(classes_path)

        # Load class and datatype descriptors
        self.reference_class_descriptors = self.load_model_descriptors(classes_paths)

        # Load MS-05 datatype descriptors
        self.reference_datatype_descriptors = self.load_model_descriptors(datatype_paths)

        # Generate MS-05 datatype schemas from MS-05 datatype descriptors
        self.datatype_schemas = self.generate_json_schemas(
            datatype_descriptors=self.reference_datatype_descriptors,
            schema_path=os.path.join(self.apis[CONTROL_API_KEY]["spec_path"], 'APIs/schemas/'))

    def create_ncp_socket(self, test):
        """Create a WebSocket client connection to Node under test. Raises NMOSTestException on error"""
        self.is12_utils.open_ncp_websocket(test, self.apis[CONTROL_API_KEY]["url"])

    def validate_descriptor(self, test, reference, descriptor, context=""):
        """Validate descriptor against reference descriptor. Raises NMOSTestException on error"""
        non_normative_keys = ['description']

        if isinstance(reference, dict):
            reference_keys = set(reference.keys())
            descriptor_keys = set(descriptor.keys())

            # compare the keys to see if any extra/missing
            key_diff = (set(reference_keys) | set(descriptor_keys)) - (set(reference_keys) & set(descriptor_keys))
            if len(key_diff) > 0:
                error_description = "Missing keys " if set(key_diff) <= set(reference_keys) else "Additional keys "
                raise NMOSTestException(test.FAIL(context + error_description + str(key_diff)))
            for key in reference_keys:
                if key in non_normative_keys and not isinstance(reference[key], dict):
                    continue
                # Check for class ID
                if key == 'classId' and isinstance(reference[key], list):
                    if reference[key] != descriptor[key]:
                        raise NMOSTestException(test.FAIL(context + "Unexpected ClassId. Expected: "
                                                          + str(reference[key])
                                                          + " actual: " + str(descriptor[key])))
                else:
                    self.validate_descriptor(test, reference[key], descriptor[key], context=context + key + "->")
        elif isinstance(reference, list):
            # Convert to dict and validate
            references = {item['name']: item for item in reference}
            descriptors = {item['name']: item for item in descriptor}

            return self.validate_descriptor(test, references, descriptors, context)
        else:
            if reference != descriptor:
                raise NMOSTestException(test.FAIL(context + 'Expected value: '
                                                  + str(reference)
                                                  + ', actual value: '
                                                  + str(descriptor)))
        return

    def _validate_schema(self, test, payload, schema, context=""):
        """Delegates to validate_schema. Raises NMOSTestExceptions on error"""
        if not schema:
            raise NMOSTestException(test.FAIL(context + "Missing schema. "))
        try:
            # Validate the JSON schema is correct
            self.validate_schema(payload, schema)
        except ValidationError as e:
            raise NMOSTestException(test.FAIL(context + "Schema validation error: " + e.message))
        except SchemaError as e:
            raise NMOSTestException(test.FAIL(context + "Schema error: " + e.message))

        return

    def get_class_manager_descriptors(self, test, class_manager_oid, property_id, role):
        response = self.get_property(test, class_manager_oid, property_id, role)

        if not response:
            return None

        # Create descriptor dictionary from response array
        # Use classId as key if present, otherwise use name
        def key_lambda(classId, name): return ".".join(map(str, classId)) if classId else name
        descriptors = {key_lambda(r.get('classId'), r['name']): r for r in response}

        return descriptors

    def validate_model_definitions(self, descriptors, schema_name, reference_descriptors):
        """Validate class manager model definitions against reference model descriptors. Returns [test result array]"""
        results = list()

        reference_descriptor_keys = sorted(reference_descriptors.keys())

        for key in reference_descriptor_keys:
            test = Test("Validate " + str(key) + " definition", "auto_" + str(key))
            try:
                if descriptors.get(key):
                    descriptor = descriptors[key]

                    # Validate the JSON schema is correct
                    self._validate_schema(test, descriptor, self.datatype_schemas[schema_name])

                    # Validate the descriptor is correct
                    self.validate_descriptor(test, reference_descriptors[key], descriptor)

                    results.append(test.PASS())
                else:
                    results.append(test.UNCLEAR("Not Implemented"))
            except NMOSTestException as e:
                results.append(e.args[0])

        return results

    def get_property(self, test, oid, property_id, context):
        try:
            return self.is12_utils.get_property(test, oid, property_id)
        except NMOSTestException as e:
            self.device_model_metadata["error"] = True
            self.device_model_metadata["error_msg"] += context \
                + "Error getting property: " \
                + str(property_id) + ": " \
                + str(e.args[0].detail) \
                + "; "
        return None

    def nc_object_factory(self, test, class_id, oid, role):
        """Create NcObject or NcBlock based on class_id"""
        # Check class id to determine if this is a block
        if len(class_id) > 1 and class_id[0] == 1 and class_id[1] == 1:
            member_descriptors = self.get_property(test, oid, NcBlockProperties.MEMBERS.value, role + ": ")
            if not member_descriptors:
                # An error has likely occured
                return None

            nc_block = NcBlock(class_id, oid, role, member_descriptors)

            for m in member_descriptors:
                child_object = self.nc_object_factory(test, m["classId"], m["oid"], m["role"])
                if child_object:
                    nc_block.add_child_object(child_object)
            return nc_block
        else:
            # Check to determine if this is a Class Manager
            if len(class_id) > 2 and class_id[0] == 1 and class_id[1] == 3 and class_id[2] == 2:
                class_descriptors = self.get_class_manager_descriptors(test,
                                                                       oid,
                                                                       NcClassManagerProperties.CONTROL_CLASSES.value,
                                                                       role + ": ")
                datatype_descriptors = self.get_class_manager_descriptors(test,
                                                                          oid,
                                                                          NcClassManagerProperties.DATATYPES.value,
                                                                          role + ": ")
                if not class_descriptors or not datatype_descriptors:
                    # An error has likely occured
                    return None

                return NcClassManager(class_id, oid, role, class_descriptors, datatype_descriptors)
            return NcObject(class_id, oid, role)

    def query_device_model(self, test):
        self.create_ncp_socket(test)
        if not self.device_model:
            self.device_model = self.nc_object_factory(test,
                                                       StandardClassIds.NCBLOCK.value,
                                                       self.is12_utils.ROOT_BLOCK_OID,
                                                       "root")
            if not self.device_model:
                raise NMOSTestException(test.FAIL("Unable to query Device Model: "
                                                  + self.device_model_metadata["error_msg"]))
        return self.device_model

    def get_manager(self, test, class_id):
        self.create_ncp_socket(test)
        device_model = self.query_device_model(test)
        members = device_model.find_members_by_class_id(class_id, include_derived=True)

        spec_link = "https://specs.amwa.tv/ms-05-02/branches/{}/docs/Managers.html"\
            .format(self.apis[CONTROL_API_KEY]["spec_branch"])

        if len(members) == 0:
            raise NMOSTestException(test.FAIL("Manager not found in Root Block.", spec_link))

        if len(members) > 1:
            raise NMOSTestException(test.FAIL("Manager MUST be a singleton.", spec_link))

        return members[0]

    def _findConstrainedProperties(self, test, block, context=""):
        results = []
        context += block.role

        class_manager = self.get_manager(test, StandardClassIds.NCCLASSMANAGER.value)
        
        block_member_descriptors = self.is12_utils.get_member_descriptors(test, block.oid, recurse=False)

        # Note that the userLabel of the block may also be changed, and therefore might be subject to runtime constraints constraints
        
        for descriptor in block_member_descriptors:
            class_descriptor = self.is12_utils.get_control_class(test,
                                                                 class_manager.oid,
                                                                 descriptor['classId'],
                                                                 include_inherited=True)
            
            # Get runtime property constraints
            object_runtime_constraints = self.is12_utils.get_property(test, descriptor['oid'], NcObjectProperties.RUNTIME_PROPERTY_CONSTRAINTS.value)

            for class_property in class_descriptor.get('properties'):
                datatype_constraints = None
                property_constraints = None
                runtime_constraint = None
                # Level 0: Datatype constraints
                if class_property.get('typeName'):
                    datatype_constraints = class_manager.datatype_descriptors.get(class_property['typeName']).get('constraints')
                # Level 1: Property constraints
                if not class_property['isReadOnly']:
                    property_constraints = class_property.get('constraints')
                # Level 3: Runtime constraints
                if object_runtime_constraints:
                    for object_runtime_constraint in object_runtime_constraints:
                        if object_runtime_constraint['propertyId']['level'] == class_property['id']['level'] and \
                            object_runtime_constraint['propertyId']['index'] == class_property['id']['index']:
                            runtime_constraint = object_runtime_constraint
                if datatype_constraints or property_constraints or runtime_constraint:
                     results.append({'oid': descriptor['oid'], 'name': context + ": " + class_descriptor['name'] + ": " + class_property['name'], 'property_id': class_property['id'], 'property_constraints': property_constraints, 'datatype_constraints': datatype_constraints, 'runtime_constraint': runtime_constraint})
        
        # Recurse through the child blocks
        for child_object in block.child_objects:
            if type(child_object) is NcBlock:
                results += (self._findConstrainedProperties(test, child_object, context + ": "))

        return results
    
    def _check_constrained_parameter(self, test, constraint_type, constraint, constrained_property, value):
        try:
            # Expect an error
            self.is12_utils.set_property(test, constrained_property['oid'], constrained_property['property_id'], value)
            self.constraint_error = True
            self.constraint_error_msg += constraint + " " + constraint_type + " constraint not enforced for " + constrained_property['name'] + "; "
        except NMOSTestException as e:
            # Expecting a parameter constraint violation
            # What kind of error should we be expecting
            pass

    def _check_parameter_constraints_number(self, test, parameter_constraint, constraint_type, constrained_property):
        # Attempt to set to a "legal" value
        minimum = parameter_constraint.minimum if parameter_constraint.minimum else 0
        maximum = parameter_constraint.maximum if parameter_constraint.maximum else sys.maxsize
        step = parameter_constraint.step if parameter_constraint.step else 1
                
        new_value = floor((((maximum - minimum) / 2) + minimum) / step) * step

        # Expect this to work OK
        self.is12_utils.set_property(test, constrained_property['oid'], constrained_property['property_id'], new_value)
                
        # Attempt to set to an "illegal" value
        if parameter_constraint.minimum is not None:
            self._check_constrained_parameter(test, constraint_type, "Minimum", constrained_property, minimum - step)

        if parameter_constraint.maximum is not None:                
            self._check_constrained_parameter(test, constraint_type, "Maximum", constrained_property, maximum + step)

        if parameter_constraint.step is not None:
            self._check_constrained_parameter(test, constraint_type, "Step", constrained_property, new_value + step / 2)

    def test_01(self, test):
        """Test all writable properties with constraints"""
        device_model = self.query_device_model(test)        

        constrained_properties = self._findConstrainedProperties(test, device_model);

        possible_properties = [{'answer_id': 'answer_'+str(i), 'display_answer': p['name'], 'resource': p} for i, p in enumerate(constrained_properties)]

        if len(possible_properties) == 0:
            
            return test.UNCLEAR("No properties with ParameterConstraints in Device Model.")

        if IS12_INTERATIVE_TESTING:
            question = """\
                        From this list of properties with parameter constraints\
                        carefully select those that can be safely altered by this test.

                        Once you have made you selection please press the 'Submit' button.
                        """
            
            selected_ids = self._invoke_testing_facade(question, possible_properties, test_type="multi_choice")['answer_response']
        
            selected_properties = [p["resource"] for p in possible_properties if p['answer_id'] in selected_ids]
        
            if len(selected_properties) == 0:
                return test.UNCLEAR("No properties with PropertyConstraints selected for testing.")
        else:
            # If non interactive test all properties
            selected_properties = [p["resource"] for p in possible_properties]

        self.constraint_error = False
        self.constraint_error_msg = ""

        for constrained_property in selected_properties:
            constraints = constrained_property.get('runtime_constraints') if constrained_property.get('runtime_constraints') else \
                constrained_property.get('property_constraints') if constrained_property.get('property_constraints') else \
                constrained_property.get('datatype_constraints')
            
            constraint_type = 'runtime' if constrained_property.get('runtime_constraints') else \
                'property' if constrained_property.get('property_constraints') else 'datatype'
            
            parameter_constraint = self.is12_utils.upcast_parameter_constraint(constraints)

            # Cache original property value
            original_value = self.is12_utils.get_property(test, constrained_property['oid'], constrained_property['property_id'])
            
            if parameter_constraint.constraint_type == "NcParameterConstraintsNumber":
                self._check_parameter_constraints_number(test, parameter_constraint, constraint_type, constrained_property)

            if parameter_constraint.constraint_type == "NcParameterConstraintsString":

                if parameter_constraint.pattern:
                    # Check legal case
                    x = Xeger(limit=parameter_constraint.max_characters if parameter_constraint.max_characters else 10)
                    new_value = x.xeger(parameter_constraint.pattern)
                    
                    # Expect this to work OK
                    self.is12_utils.set_property(test, constrained_property['oid'], constrained_property['property_id'], new_value)

                if parameter_constraint.pattern:
                    # Possible negative example strings
                    # Ideally we would compute a negative string based on the regex.
                    # In the meantime, some strings that might possibly violate the regex
                    negative_examples = ['!$%^&*()+_:;/', '*********', '000000000', 'AAAAAAAA']
                    
                    for negative_example in negative_examples:
                        # Verify this string violates constraint
                        if not re.search(parameter_constraint.pattern, negative_example):
                            self._check_constrained_parameter(test, constraint_type, "Pattern", constrained_property, new_value)
                            
                    # Exceed max character limit
                if parameter_constraint.max_characters:
                    if parameter_constraint.pattern:
                        x = Xeger(limit=parameter_constraint.max_characters * 2)
                        new_value = x.xeger(parameter_constraint.pattern)
                    else:
                        new_value = '*' * parameter_constraint.max_characters * 2
                    
                    # Verfiy this string violates constraint
                    if len(new_value) > parameter_constraint.max_characters:
                        self._check_constrained_parameter(test, constraint_type, "Max characters", constrained_property, new_value)

            # Reset to original value
            self.is12_utils.set_property(test, constrained_property['oid'], constrained_property['property_id'], original_value)

        if self.constraint_error:
            return test.FAIL(self.constraint_error_msg)
        
        return test.PASS()

