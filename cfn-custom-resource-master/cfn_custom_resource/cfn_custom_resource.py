"""Base class for implementing Lambda functions backing custom CloudFormation resources.

The class, CloudFormationCustomResource, has methods that child classes
implement to create, update, or delete the resource, while taking care of the
parsing of the input, exception handling, and response sending.

# For resources of type Custom::MyCustomResource
class MyCustomResource(CloudFormationCustomResource):
    def create(self):
        # Implement
        # For AWS SDKs, use:
        #   self.get_boto3_client(service_name)
        #   self.get_boto3_resource(service_name)
        # Set the name of what you are creating to the value in
        #   self.physical_resource_id
        # This id is autogenerated for you, but you can set it if you want or need
        # This id is what CloudFormation uses for Ref
        # The resource properties defined in the template are in
        #   self.resource_properties
        # Attributes can be set by returning a dictionary

    def update(self):
        # Implement
        # The name of what you previously created is
        #   self.physical_resource_id
        # The updated properties are in
        #   self.resource_properties
        # To check what's changed, compare with
        #   self.old_resource_properties
        # If you set attributes in create(), you need to set them here too  

    def delete(self):
        # implement

This Source Code Form is subject to the terms of the Mozilla Public
License, v. 2.0. If a copy of the MPL was not distributed with this
file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""

import logging
import json
import traceback
import random
import string
import six

from six.moves import http_client

import boto3
import requests

class CloudFormationCustomResource(object):
    """Base class for CloudFormation custom resource classes.

    To create a handler for a custom resource in CloudFormation, simply create a
    child class (say, MyCustomResource), implement the methods specified below,
    and create the handler function:

    handler = MyCustomResource.get_handler()

    By default, the class name is assumed to be the expected resource type name
    (i.e., Custom:: + the class name), and incoming requests are validated against
    this. To disable this, set a class field RESOURCE_TYPE_SPEC=None. To validate
    incoming requests against types other than the class name, set
    RESOURCE_TYPE_SPEC to be a string or a list of strings.

    Child classes must implement the create(), update(), and delete() methods.
    Each of these methods can indicate success or failure in one of two ways:
    * Simply return or raise an exception
    * Set self.status to self.STATUS_SUCCESS or self.STATUS_FAILED
        In the case of failure, self.failure_reason can be set to a string to
        provide an explanation in the response.
    These methods can also populate the self.resource_outputs dictionary with fields
    that then will be available in CloudFormation. If the return value of the function
    is a dict, that is merged into resource_outputs. If it is not a dict, the value
    is stored under the 'result' key.
    
    By default, before create is called, self.physical_resource_id is set to a value
    that is generated similar to how CloudFormation does it:
    {stack_id}-{logical resource id}-{random string}. This value should be used to
    name the resource being created.
    If instead, the physical resource id is generated by the resource itself
    (for example, Cognito pools have a unique id generated by Cognito), then you
    should set the class field DISABLE_PHYSICAL_RESOURCE_ID_GENERATION=True, and 
    set self.physical_resource_id yourself within the create method.

    Child classes may implement validate() and/or populate(). validate() should return
    True if self.resource_properties is valid. populate() can transfer the contents of
    self.resource_properties into object fields, if this is not done by validate().

    The class provides methods get_boto3_client() and get_boto3_resource() that cache
    the clients/resources in the class, reducing overhead in the Lambda invocations.
    These also rely on the get_boto3_session() method, which in turn uses
    BOTO3_SESSION_FACTORY if it is set, allowing overriding with mock sessions for
    testing. Similarly, BOTO3_CLIENT_FACTORY and BOTO3_RESOURCE_FACTORY, both of which
    can be set to callables that take a session and a name, can be set to override
    client and resource creation.

    Some hooks are provided to override behavior. The first four are instance fields,
    since they may be set to functions that rely on instance fields. The last
    is a class field, since it is called by a class method.
    * finish_function, normally set to CloudFormationCustomResource.cfn_response, takes
        as input the custom resource object and deals with sending the response and
        cleaning up.
    * send_function, used within CloudFormationCustomResource.cfn_response, takes as
        input the custom resource object, a url, and the response_content dictionary.
        Normally this is set to CloudFormationCustomResource.send_response, which uses
        requests to send the content to its destination.
    * generate_unique_id_prefix_function can be set to put a prefix on the id returned
        by generate_unique_id, for example if the physical resource
        id needs to be an ARN.
    * generate_physical_resource_id_function is used to get a physical resource id
        on a create call unless DISABLE_PHYSICAL_RESOURCE_ID_GENERATION is True.
        It takes the custom resource object as input.This is normally
        set to CloudFormationCustomResource.generate_unique_id, which
        generates a physical resource id like CloudFormation:
        {stack_id}-{logical resource id}-{random string}
        It also provides two keyword arguments:
        * prefix: if for example the physical resource id must be an arn
        * separator: defaulting to '-'.
    * BOTO3_SESSION_FACTORY takes no input and returns an object that acts like a boto3 session.
        If this class field is not None, it is used by get_boto3_session() instead of creating
        a regular boto3 session. This could be made to use placebo for testing
        https://github.com/garnaat/placebo

    The class provides four configuration options that can be overridden in child
    classes:
    * DELETE_LOGS_ON_STACK_DELETION: A boolean which, when True, will cause a successful
        stack deletion to trigger the deletion of the CloudWatch log group on stack
        deletion. If there is a problem during stack deletion, the logs are left in place.
        NOTE: this is not intended for use when the Lambda function is used by multiple
        stacks.
    * DISABLE_PHYSICAL_RESOURCE_ID_GENERATION: If True, skips the automatic generation
        of a unique physical resource id if the custom resource has a source for that
        itself, for example if it is specified in the properties, or returned by the
        resource creation API. Using this option, the class must set
        self.physical_resource_id in the create() method.
    * PHYSICAL_RESOURCE_ID_MAX_LEN: An int used by generate_unique_id
        when generating a physical resource id.
    """
    DELETE_LOGS_ON_STACK_DELETION = False

    DISABLE_PHYSICAL_RESOURCE_ID_GENERATION = False
    PHYSICAL_RESOURCE_ID_MAX_LEN = 128

    STATUS_SUCCESS = 'SUCCESS'
    STATUS_FAILED = 'FAILED'

    REQUEST_CREATE = 'Create'
    REQUEST_DELETE = 'Delete'
    REQUEST_UPDATE = 'Update'

    BASE_LOGGER_LEVEL = None
    
    DUMMY_RESPONSE_URL_SILENT = 'dummy:silent'
    DUMMY_RESPONSE_URL_PRINT = 'dummy:print'
    RAISE_ON_FAILURE = False

    STRINGIFY_OUTPUT = True

    def __init__(self, logger=None):
        if logger:
            self.logger = logger
        else:
            self.logger = logging.getLogger(self.__class__.__name__)

        self._base_logger = logging.getLogger('CloudFormationCustomResource')
        if self.BASE_LOGGER_LEVEL:
            self._base_logger.setLevel(self.BASE_LOGGER_LEVEL)

        resource_type_spec = getattr(self, 'RESOURCE_TYPE_SPEC', self.__class__.__name__)

        def process_resource_type_spec(resource_type_spec):
            if not (resource_type_spec.startswith('Custom::') or resource_type_spec == 'AWS::CloudFormation::CustomResource'):
                resource_type_spec = 'Custom::' + resource_type_spec
            return resource_type_spec

        if isinstance(resource_type_spec, (list, tuple)):
            resource_type_spec = [process_resource_type_spec(rt) for rt in resource_type_spec]
        elif isinstance(resource_type_spec, six.string_types):
            resource_type_spec = process_resource_type_spec(resource_type_spec)

        self.resource_type_spec = resource_type_spec

        self.event = None
        self.context = None

        self.resource_type = None
        self.request_type = None
        self.response_url = None
        self.stack_id = None
        self.request_id = None

        self.logical_resource_id = None
        self.physical_resource_id = None
        self.resource_properties = None
        self.old_resource_properties = None

        self.status = None
        self.failure_reason = None
        self.resource_outputs = {}

        self.finish_function = self.cfn_response
        self.send_response_function = self.send_response

        self.generate_unique_id_prefix_function = None
        self.generate_physical_resource_id_function = self.generate_unique_id

    def validate_resource_type(self, resource_type):
        """Return True if resource_type is valid"""
        if not self.resource_type_spec:
            return True
        if isinstance(self.resource_type_spec, (list, tuple)):
            return resource_type in self.resource_type_spec
        return resource_type == self.resource_type_spec

    def validate(self):
        """Return True if self.resource_properties is valid."""
        return True

    def populate(self):
        """Populate subclass fields from self.resource_properties and
        self.old_resource_properties, if this is not done in validate()"""
        pass

    def create(self):
        raise NotImplementedError

    def update(self):
        raise NotImplementedError
    
    def delete(self):
        raise NotImplementedError

    def has_property_changed(self, property_name):
        """Test if a property has changed. Will return true during create."""
        return (self.old_resource_properties is None
            or self.old_resource_properties.get(property_name) != self.resource_properties.get(property_name)) 

    BOTO3_SESSION_FACTORY = None
    BOTO3_CLIENT_FACTORY = None
    BOTO3_RESOURCE_FACTORY = None

    BOTO3_SESSION = None
    BOTO3_CLIENTS = {}
    BOTO3_RESOURCES = {}

    @classmethod
    def get_boto3_session(cls):
        if cls.BOTO3_SESSION is None:
            if cls.BOTO3_SESSION_FACTORY:
                cls.BOTO3_SESSION = cls.BOTO3_SESSION_FACTORY()
            else:
                cls.BOTO3_SESSION = boto3.session.Session()
        return cls.BOTO3_SESSION

    @classmethod
    def get_boto3_client(cls, name):
        if name not in cls.BOTO3_CLIENTS:
            if cls.BOTO3_CLIENT_FACTORY:
                client = cls.BOTO3_CLIENT_FACTORY(cls.get_boto3_session(), name)
            else:
                client = cls.get_boto3_session().client(name)
            cls.BOTO3_CLIENTS[name] = client
        return cls.BOTO3_CLIENTS[name]

    @classmethod
    def get_boto3_resource(cls, name):
        if name not in cls.BOTO3_RESOURCES:
            if cls.BOTO3_RESOURCE_FACTORY:
                resource = cls.BOTO3_RESOURCE_FACTORY(cls.get_boto3_session(), name)
            else:
                resource = cls.get_boto3_session().resource(name)
            cls.BOTO3_RESOURCES[name] = resource
        return cls.BOTO3_RESOURCES[name]

    @classmethod
    def get_handler(cls, *args, **kwargs):
        """Returns a handler suitable for Lambda to call. The handler creates an
        instance of the class in every call, passing any arguments given to
        get_handler.

        Use like:
        handler = MyCustomResource.get_handler()"""
        def handler(event, context):
            return cls(*args, **kwargs).handle(event, context)
        return handler

    def handle(self, event, context):
        """Use the get_handler class method to get a handler that calls this method."""
        self._base_logger.info('REQUEST RECEIVED: {}'.format(json.dumps(event)))
        def plainify(obj):
            d = {}
            for field, value in six.iteritems(vars(obj)):
                if field.startswith('_'):
                    continue
                if isinstance(value, (float, bool, type(None)) + six.integer_types + six.string_types):
                    d[field] = value
                elif isinstance(value, (list, tuple)):
                    d[field] = [plainify(v) for v in value]
                elif isinstance(value, dict):
                    d[field] = dict((k, plainify(v)) for k, v in six.iteritems(value))
                else:
                    d[field] = repr(value)
            return d
        self._base_logger.info('LambdaContext: %s' % json.dumps(plainify(context)))

        # handle an event nested inside of an SNS event
        if 'Records' in event and len(event['Records']) == 1:
            event = json.loads(event['Records'][0]['Sns']['Message'])

        self.event = event
        self.context = context

        self.resource_type = event['ResourceType']
        self.request_type = event['RequestType']
        self.response_url = event['ResponseURL']
        self.stack_id = event['StackId']
        self.request_id = event['RequestId']

        self.logical_resource_id = event['LogicalResourceId']
        self.physical_resource_id = event.get('PhysicalResourceId')
        self.resource_properties = event.get('ResourceProperties', {})
        self.old_resource_properties = event.get('OldResourceProperties')

        self.status = None
        self.failure_reason = None
        self.resource_outputs = {}

        try:
            if not self.validate_resource_type(self.resource_type):
                raise Exception('invalid resource type')

            if not self.validate():
                pass

            if not self.physical_resource_id and not self.DISABLE_PHYSICAL_RESOURCE_ID_GENERATION:
                self.physical_resource_id = self.generate_physical_resource_id_function(max_len=self.PHYSICAL_RESOURCE_ID_MAX_LEN)

            self.populate()

            method_name = self.request_type.lower()
            self._base_logger.debug("Dispatching to subclass: {}".format(method_name))
            outputs = getattr(self, method_name)()

            if outputs:
                if not isinstance(outputs, dict):
                    outputs = {'Value': outputs}
                self.resource_outputs.update(outputs)

            if not self.status:
                self.status = self.STATUS_SUCCESS
         
        except Exception as e:
            if not self.status:
                self.status = self.STATUS_FAILED
                self.failure_reason = 'Custom resource {} failed due to exception "{}".'.format(self.__class__.__name__, e)
            if self.failure_reason:
                self._base_logger.error(str(self.failure_reason))
            self._base_logger.debug(traceback.format_exc())

        if self.request_type == self.REQUEST_DELETE:
            if self.status == self.STATUS_SUCCESS and self.DELETE_LOGS_ON_STACK_DELETION:
                logging.disable(logging.CRITICAL)
                logs_client = self.get_boto3_client('logs')
                logs_client.delete_log_group(
                    logGroupName=context.log_group_name)

        self.finish_function(self)

    def generate_unique_id(self, prefix=None, separator='-', max_len=None):
        """Generate a unique id similar to how CloudFormation generates
        physical resource ids"""

        if prefix is None:
            if self.generate_unique_id_prefix_function:
                prefix = self.generate_unique_id_prefix_function()
            else:
                prefix = ''

        stack_id = self.stack_id.split(':')[-1]
        if '/' in stack_id:
            stack_id = stack_id.split('/')[1]
        stack_id = stack_id.replace('-', '')

        logical_resource_id = self.logical_resource_id

        len_of_rand = 12

        rand = ''.join(random.choice(string.ascii_uppercase + string.digits) for _ in range(len_of_rand))

        if max_len:
            max_len = max_len - len(prefix)
            len_of_parts = max_len - len_of_rand - 2 * len(separator)
            len_of_parts_diff = (len(stack_id) + len(logical_resource_id)) - len_of_parts
            if len_of_parts_diff > 0:
                len_of_stack_id = min(len(stack_id), len(stack_id) - len_of_parts_diff // 2)
                len_of_resource = len_of_parts - len_of_stack_id
                stack_id = stack_id[:len_of_stack_id]
                logical_resource_id = logical_resource_id[:len_of_resource]
        return '{prefix}{stack_id}{separator}{logical_id}{separator}{rand}'.format(
            prefix=prefix,
            separator=separator,
            stack_id=stack_id,
            logical_id=logical_resource_id,
            rand=rand,
            )

    @classmethod
    def send_response(cls, resource, url, response_content):
        if url == cls.DUMMY_RESPONSE_URL_SILENT:
            return
        elif url == cls.DUMMY_RESPONSE_URL_PRINT:
            six.print_(json.dumps(response_content, indent=2))
        else:
            put_response = requests.put(url,
                                        data=json.dumps(response_content))
            status_code = put_response.status_code
            response_text = put_response.text
            
            body_text = ""
            if status_code // 100 != 2:
                body_text = "\n" + response_text
            resource._base_logger.debug("Status code: {} {}{}".format(put_response.status_code, http_client.responses[put_response.status_code], body_text))

        return put_response

    @classmethod
    def cfn_response(cls, resource):
        physical_resource_id = resource.physical_resource_id
        if physical_resource_id is None:
            physical_resource_id = resource.context.log_stream_name
        default_reason = ("See the details in CloudWatch Log Stream: {}".format(resource.context.log_stream_name))
        outputs = {}
        for key, value in six.iteritems(resource.resource_outputs):
            if resource.STRINGIFY_OUTPUT and not isinstance(value, six.string_types):
                value = json.dumps(value)
            outputs[key] = value
        response_content = {
            "Status": resource.status,
            "Reason": resource.failure_reason or default_reason,
            "PhysicalResourceId": physical_resource_id,
            "StackId": resource.event['StackId'],
            "RequestId": resource.event['RequestId'],
            "LogicalResourceId": resource.event['LogicalResourceId'],
            "Data": outputs
        }
        resource._base_logger.debug("Response body: {}".format(json.dumps(response_content)))
        if cls.RAISE_ON_FAILURE and resource.status == cls.STATUS_FAILED:
            raise Exception(resource.failure_reason)
        try:
            return resource.send_response_function(resource, resource.response_url, response_content)
        except Exception as e:
            resource._base_logger.error("send response failed: {}".format(e))
            resource._base_logger.debug(traceback.format_exc())
