"""
Integration tests for the stem.control.BaseController class.
"""

import re
import time
import unittest
import threading

import stem.control
import stem.socket
import test.runner

class StateObserver:
  """
  Simple container for listening to ControlSocket state changes and
  rembembering them for the test.
  """
  
  controller = None
  state = None
  timestamp = None
  
  def reset(self):
    self.controller = None
    self.state = None
    self.timestamp = None
  
  def listener(self, controller, state, timestamp):
    self.controller = controller
    self.state = state
    self.timestamp = timestamp

class TestBaseController(unittest.TestCase):
  def setUp(self):
    test.runner.require_control(self)
  
  def test_from_port(self):
    """
    Basic sanity check for the from_port constructor.
    """
    
    if test.runner.Torrc.PORT in test.runner.get_runner().get_options():
      with stem.control.BaseController.from_port(control_port = test.runner.CONTROL_PORT) as controller:
        self.assertTrue(isinstance(controller, stem.control.BaseController))
    else:
      self.assertRaises(stem.socket.SocketError, stem.control.BaseController.from_port, "127.0.0.1", test.runner.CONTROL_PORT)
  
  def test_from_socket_file(self):
    """
    Basic sanity check for the from_socket_file constructor.
    """
    
    if test.runner.Torrc.SOCKET in test.runner.get_runner().get_options():
      with stem.control.BaseController.from_socket_file(socket_path = test.runner.CONTROL_SOCKET_PATH) as controller:
        self.assertTrue(isinstance(controller, stem.control.BaseController))
    else:
      self.assertRaises(stem.socket.SocketError, stem.control.BaseController.from_socket_file, test.runner.CONTROL_SOCKET_PATH)
  
  def test_connect_repeatedly(self):
    """
    Connects and closes the socket repeatedly. This is a simple attempt to
    trigger concurrency issues.
    """
    
    with test.runner.get_runner().get_tor_socket() as control_socket:
      controller = stem.control.BaseController(control_socket)
      
      for i in xrange(250):
        controller.connect()
        controller.close()
  
  def test_msg(self):
    """
    Tests a basic query with the msg() method.
    """
    
    runner = test.runner.get_runner()
    with runner.get_tor_socket() as control_socket:
      controller = stem.control.BaseController(control_socket)
      response = controller.msg("GETINFO version")
      
      tor_version = runner.get_tor_version()
      self.assertEquals("version=%s\nOK" % tor_version, str(response))
  
  def test_msg_invalid(self):
    """
    Tests the msg() method against an invalid controller command.
    """
    
    with test.runner.get_runner().get_tor_socket() as control_socket:
      controller = stem.control.BaseController(control_socket)
      response = controller.msg("invalid")
      self.assertEquals('Unrecognized command "invalid"', str(response))
  
  def test_msg_invalid_getinfo(self):
    """
    Tests the msg() method against a non-existant GETINFO option.
    """
    
    with test.runner.get_runner().get_tor_socket() as control_socket:
      controller = stem.control.BaseController(control_socket)
      response = controller.msg("GETINFO blarg")
      self.assertEquals('Unrecognized key "blarg"', str(response))
  
  def test_msg_repeatedly(self):
    """
    Connects, sends a burst of messages, and closes the socket repeatedly. This
    is a simple attempt to trigger concurrency issues.
    """
    
    with test.runner.get_runner().get_tor_socket() as control_socket:
      controller = stem.control.BaseController(control_socket)
      
      def run_getinfo():
        for i in xrange(150):
          try:
            controller.msg("GETINFO version")
            controller.msg("GETINFO blarg")
            controller.msg("blarg")
          except stem.socket.ControllerError:
            pass
      
      message_threads = []
      
      for i in xrange(5):
        msg_thread = threading.Thread(target = run_getinfo)
        message_threads.append(msg_thread)
        msg_thread.setDaemon(True)
        msg_thread.start()
      
      for i in xrange(100):
        controller.connect()
        controller.close()
      
      for msg_thread in message_threads:
        msg_thread.join()
  
  def test_asynchronous_event_handling(self):
    """
    Check that we can both receive asynchronous events while hammering our
    socket with queries, and checks that when a controller is closed the
    listeners will still receive all of the enqueued events.
    """
    
    class ControlledListener(stem.control.BaseController):
      """
      Controller that blocks event handling until told to do so.
      """
      
      def __init__(self, control_socket):
        stem.control.BaseController.__init__(self, control_socket)
        self.received_events = []
        self.receive_notice = threading.Event()
      
      def _handle_event(self, event_message):
        self.receive_notice.wait()
        self.received_events.append(event_message)
    
    with test.runner.get_runner().get_tor_socket() as control_socket:
      controller = ControlledListener(control_socket)
      controller.msg("SETEVENTS BW")
      
      # Wait for a couple events for events to be enqueued. Doing a bunch of
      # GETINFO queries while waiting to better exercise the asynchronous event
      # handling.
      
      start_time = time.time()
      
      while (time.time() - start_time) < 2:
        test.runner.exercise_controller(self, controller)
      
      # Concurrently shut down the controller. We need to do this in another
      # thread because it'll block on the event handling, which in turn is
      # currently blocking on the reveive_notice.
      
      close_thread = threading.Thread(target = controller.close, name = "Closing controller")
      close_thread.setDaemon(True)
      close_thread.start()
      
      # Finally start handling the BW events that we've received. We should
      # have at least a couple of them.
      
      controller.receive_notice.set()
      close_thread.join()
      
      self.assertTrue(len(controller.received_events) >= 2)
      
      for bw_event in controller.received_events:
        self.assertTrue(re.match("BW [0-9]+ [0-9]+", str(bw_event)))
        self.assertTrue(re.match("650 BW [0-9]+ [0-9]+\r\n", bw_event.raw_content()))
        self.assertEquals(("650", " "), bw_event.content()[0][:2])
  
  def test_status_notifications(self):
    """
    Checks basic functionality of the add_status_listener() and
    remove_status_listener() methods.
    """
    
    state_observer = StateObserver()
    
    with test.runner.get_runner().get_tor_socket(False) as control_socket:
      controller = stem.control.BaseController(control_socket)
      controller.add_status_listener(state_observer.listener, False)
      
      controller.close()
      self.assertEquals(controller, state_observer.controller)
      self.assertEquals(stem.control.State.CLOSED, state_observer.state)
      self.assertTrue(state_observer.timestamp < time.time())
      self.assertTrue(state_observer.timestamp > time.time() - 1.0)
      state_observer.reset()
      
      controller.connect()
      self.assertEquals(controller, state_observer.controller)
      self.assertEquals(stem.control.State.INIT, state_observer.state)
      self.assertTrue(state_observer.timestamp < time.time())
      self.assertTrue(state_observer.timestamp > time.time() - 1.0)
      state_observer.reset()
      
      # cause the socket to shut down without calling close()
      controller.msg("Blarg!")
      self.assertRaises(stem.socket.SocketClosed, controller.msg, "blarg")
      self.assertEquals(controller, state_observer.controller)
      self.assertEquals(stem.control.State.CLOSED, state_observer.state)
      self.assertTrue(state_observer.timestamp < time.time())
      self.assertTrue(state_observer.timestamp > time.time() - 1.0)
      state_observer.reset()
      
      # remove listener and make sure we don't get further notices
      controller.remove_status_listener(state_observer.listener)
      controller.connect()
      self.assertEquals(None, state_observer.controller)
      self.assertEquals(None, state_observer.state)
      self.assertEquals(None, state_observer.timestamp)
      state_observer.reset()
      
      # add with spawn as true, we need a little delay on this since we then
      # get the notice asynchronously
      
      controller.add_status_listener(state_observer.listener, True)
      controller.close()
      time.sleep(0.1) # not much work going on so this doesn't need to be much
      self.assertEquals(controller, state_observer.controller)
      self.assertEquals(stem.control.State.CLOSED, state_observer.state)
      self.assertTrue(state_observer.timestamp < time.time())
      self.assertTrue(state_observer.timestamp > time.time() - 1.0)
      state_observer.reset()

