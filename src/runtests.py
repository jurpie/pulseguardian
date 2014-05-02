# Any copyright is dedicated to the Public Domain.
# http://creativecommons.org/publicdomain/zero/1.0/

import Queue
import multiprocessing
import time
import unittest
import uuid

from mozillapulse import consumers, publishers
from mozillapulse.messages.base import GenericMessage

from management import PulseManagementAPI
from guardian import PulseGuardian

from model.user import User

# Default RabbitMQ host settings are as defined in the accompanying
# vagrant puppet files.
DEFAULT_RABBIT_HOST = 'localhost'
DEFAULT_RABBIT_PORT = 5672
DEFAULT_RABBIT_VHOST = '/'
DEFAULT_RABBIT_USER = 'guest'
DEFAULT_RABBIT_PASSWORD = 'guest'

CONSUMER_USER = 'guardtest'
CONSUMER_PASSWORD = 'guardtest'


# Global pulse configuration.
pulse_cfg = {}


class ConsumerSubprocess(multiprocessing.Process):

    def __init__(self, consumer_class, config, durable=False):
        multiprocessing.Process.__init__(self)
        self.consumer_class = consumer_class
        self.config = config
        self.durable = durable
        self.queue = multiprocessing.Queue()

    def run(self):
        queue = self.queue
        def cb(body, message):
            queue.put(body)
            message.ack()
        consumer = self.consumer_class(durable=self.durable, **self.config)
        consumer.configure(topic='#', callback=cb)
        consumer.listen()


class GuardianProcess(multiprocessing.Process):

    def __init__(self, management_api):
        multiprocessing.Process.__init__(self)
        self.management_api = management_api
        self.guardian = PulseGuardian(self.management_api)

    def run(self):
        self.guardian.guard()

class PulseTestMixin(object):

    """Launches a consumer in a separate process and publishes a message in
    the main process.  The consumer will send the received message back
    to the main process for validation.  We use processes instead of threads
    since it's easier to kill a process (the listen() call cannot be
    terminated otherwise).
    """

    proc = None

    # Override these.
    consumer = None
    publisher = None

    QUEUE_CHECK_PERIOD = 0.05
    QUEUE_CHECK_ATTEMPTS = 4000

    def _build_message(self, msg_id):
        raise NotImplementedError()

    def setUp(self):
        self.management_api = PulseManagementAPI()
        self.guardian = GuardianProcess(self.management_api)

    def tearDown(self):
        self.terminate_proc()

    def terminate_proc(self):
        if self.proc:
            self.proc.terminate()
            self.proc.join()
            self.proc = None

    def _wait_for_queue(self, config, queue_should_exist=True):
        # Wait until queue has been created by consumer process.
        consumer = self.consumer(**config)
        consumer.configure(topic='#', callback=lambda x, y: None)
        attempts = 0
        while attempts < self.QUEUE_CHECK_ATTEMPTS:
            attempts += 1
            if consumer.queue_exists() == queue_should_exist:
                break
            time.sleep(self.QUEUE_CHECK_PERIOD)
        self.assertEqual(consumer.queue_exists(), queue_should_exist)

    def _get_verify_msg(self, msg):
        try:
            received_data = self.proc.queue.get(timeout=5)
        except Queue.Empty:
            self.fail('did not receive message from consumer process')
        self.assertEqual(msg.routing_key, received_data['_meta']['routing_key'])
        received_payload = {}
        for k, v in received_data['payload'].iteritems():
            received_payload[k.encode('ascii')] = v.encode('ascii')
        self.assertEqual(msg.data, received_payload)

    def test_durable(self):
        self.guardian.start()

        publisher = self.publisher(**pulse_cfg)
        for i in xrange(20):
            msg = self._build_message(i)
            publisher.publish(msg)

        consumer_cfg = pulse_cfg.copy()
        consumer_cfg['applabel'] = str(uuid.uuid1())

        consumer_cfg['user'], consumer_cfg['password'] = CONSUMER_USER, CONSUMER_PASSWORD
        username, password = consumer_cfg['user'], consumer_cfg['password']


        user = User.query.filter(User.username == username).first()
        if user is None:
            user = User.new_user(username=username, email='akachkach@mozilla.com', password=password)
            user.activate(self.management_api)

        self.proc = ConsumerSubprocess(self.consumer, consumer_cfg, True)
        self.proc.start()

        self._wait_for_queue(consumer_cfg)
        self.terminate_proc()

        # Queue should still exist.
        self._wait_for_queue(consumer_cfg)

        # Queue multiple messages while no consumer exists.
        for i in xrange(20):
            msg = self._build_message('2')
            publisher.publish(msg)

        # Message should be immediately available when a new consumer is
        # created and hooked up to the original queue.
        self.proc = ConsumerSubprocess(self.consumer, consumer_cfg, True)
        self.proc.start()
        self._get_verify_msg(msg)
        time.sleep(100)
        self.terminate_proc()

        msg = self._build_message('3')
        publisher.publish(msg)

        self.guardian.terminate()
        self.guardian.join()

        # # Purge messages and add a new one.
        # consumer = self.consumer(**consumer_cfg)
        # consumer.configure(topic='#', callback=lambda x, y: None)
        # consumer.purge_existing_messages()
        # msg = self._build_message('4')
        # publisher.publish(msg)

        # # When a new consumer reads from the original queue, only the last
        # # message should be available.
        # self.proc = ConsumerSubprocess(self.consumer, consumer_cfg, True)
        # self.proc.start()
        # self._get_verify_msg(msg)
        # self.terminate_proc()

        # # Delete the queue.
        # consumer = self.consumer(**consumer_cfg)
        # consumer.configure(topic='#', callback=lambda x, y: None)
        # consumer.delete_queue()
        # self._wait_for_queue(consumer_cfg, False)


class TestMessage(GenericMessage):

    def __init__(self):
        super(TestMessage, self).__init__()
        self.routing_parts.append('test')

class GuardianTest(PulseTestMixin, unittest.TestCase):

    consumer = consumers.PulseTestConsumer
    publisher = publishers.PulseTestPublisher

    def _build_message(self, msg_id):
        msg = TestMessage()
        msg.set_data('id', msg_id)
        # msg.set_data('when', '1369685091')
        # msg.set_data('who', 'somedev@mozilla.com')
        return msg


def main(pulse_opts):
    global pulse_cfg
    pulse_cfg.update(pulse_opts)
    unittest.main()


if __name__ == '__main__':
    from optparse import OptionParser
    parser = OptionParser()
    parser.add_option('--host', action='store', dest='host',
                      default=DEFAULT_RABBIT_HOST,
                      help='host running RabbitMQ; defaults to %s' %
                      DEFAULT_RABBIT_HOST)
    parser.add_option('--port', action='store', type='int', dest='port',
                      default=DEFAULT_RABBIT_PORT,
                      help='port on which RabbitMQ is running; defaults to %d'
                      % DEFAULT_RABBIT_PORT)
    parser.add_option('--vhost', action='store', dest='vhost',
                      default=DEFAULT_RABBIT_VHOST,
                      help='name of pulse vhost; defaults to "%s"' %
                      DEFAULT_RABBIT_VHOST)
    parser.add_option('--user', action='store', dest='user',
                      default=DEFAULT_RABBIT_USER,
                      help='name of pulse RabbitMQ user; defaults to "%s"' %
                      DEFAULT_RABBIT_USER)
    parser.add_option('--password', action='store', dest='password',
                      default=DEFAULT_RABBIT_PASSWORD,
                      help='password of pulse RabbitMQ user; defaults to "%s"'
                      % DEFAULT_RABBIT_PASSWORD)
    (opts, args) = parser.parse_args()
    main(opts.__dict__)