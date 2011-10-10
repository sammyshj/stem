"""
Functions for connecting and authenticating to the tor process.
"""

import Queue
import socket
import threading

from stem.util import log

class ProtocolError(Exception):
  "Malformed content from the control socket."
  pass

class ControlMessage:
  """
  Represents a complete message from the control socket.
  """
  
  def __init__(self, lines, raw_content):
    if not lines: raise ValueError("Control messages can't be empty")
    
    # Parsed control message. This is a list of tuples with the form...
    # (status code, divider, content)
    self._lines = lines
    
    # String with the unparsed content read from the control port.
    self._raw_content = raw_content
  
  def get_raw_content(self):
    """
    Provides the unparsed content read from the control socket.
    
    Returns:
      string of the socket data used to generate this message
    """
    
    return self._raw_content
  
  def get_status_code(self, line = -1):
    """
    Provides the status code for a line of the message.
    
    Arguments:
      line - line for which the status code is returned
    
    Returns:
      string status code for the line
    """
    
    return self._lines[line][0]
  
  def __str__(self):
    return "\n".join(list(self))
  
  def __iter__(self):
    """
    Provides the parsed content of the message, not including the status codes
    and dividers.
    """
    
    for _, _, content in self._lines:
      for content_line in content.split("\n"):
        yield content_line

class ControlConnection:
  """
  Connection to a Tor control port. This is a very lightweight wrapper around
  the socket, providing basic process communication and event listening. Don't
  use this directly - subclasses provide frendlier controller access.
  """
  
  def __init__(self, control_socket):
    self._is_running = True
    self._control_socket = control_socket
    
    # File accessor for far better sending and receiving functionality. This
    # uses a duplicate file descriptor so both this and the socket need to be
    # closed when done.
    
    self._control_socket_file = self._control_socket.makefile()
    
    # queues where messages from the control socket are directed
    self._event_queue = Queue.Queue()
    self._reply_queue = Queue.Queue()
    
    # prevents concurrent writing to the socket
    self._socket_write_cond = threading.Condition()
    
    # thread to pull from the _event_queue and call handle_event
    self._event_cond = threading.Condition()
    self._event_thread = threading.Thread(target = self._event_loop)
    self._event_thread.setDaemon(True)
    self._event_thread.start()
    
    # thread to continually pull from the control socket
    self._reader_thread = threading.Thread(target = self._reader_loop)
    self._reader_thread.setDaemon(True)
    self._reader_thread.start()
  
  def is_running(self):
    """
    True if we still have an open connection to the control socket, false
    otherwise.
    """
    
    return self._is_running
  
  def handle_event(self, event_message):
    """
    Overwritten by subclasses to provide event listening. This is notified
    whenever we receive an event from the control socket.
    
    Arguments:
      event_message (ControlMessage) - message received from the control socket
    """
    
    pass
  
  def send(self, message):
    """
    Sends a message to the control socket and waits for a reply.
    
    Arguments:
      message (str) - message to be sent to the control socket
    
    Returns:
      ControlMessage with the response from the control socket
    """
    
    # makes sure that the message ends with a CRLF
    message = message.rstrip("\r\n") + "\r\n"
    
    self._socket_write_cond.acquire()
    self._control_socket_file.write(message)
    self._control_socket_file.flush()
    self._socket_write_cond.release()
    
    return self._reply_queue.get()
  
  def _event_loop(self):
    """
    Continually pulls messages from the _event_thread and sends them to
    handle_event. This is done via its own thread so subclasses with a lenghty
    handle_event implementation don't block further reading from the socket.
    """
    
    while self.is_running():
      try:
        event_message = self._event_queue.get_nowait()
        self.handle_event(event_message)
      except Queue.Empty:
        self._event_cond.acquire()
        self._event_cond.wait()
        self._event_cond.release()
  
  def _reader_loop(self):
    """
    Continually pulls from the control socket, directing the messages into
    queues based on their type. Controller messages come in two varieties...
    
    - Responses to messages we've sent (GETINFO, SETCONF, etc).
    - Asynchronous events, identified by a status code of 650.
    """
    
    while self.is_running():
      try:
        control_message = self._read_message()
        
        if control_message.get_status_code() == "650":
          # adds this to the event queue and wakes up the handler
          
          self._event_cond.acquire()
          self._event_queue.put(control_message)
          self._event_cond.notifyAll()
          self._event_cond.release()
        else:
          # TODO: figure out a good method for terminating the socket thread
          self._reply_queue.put(control_message)
      except ProtocolError, exc:
        log.log(log.ERR, "Error reading control socket message: %s" % exc)
        # TODO: terminate?
  
  def _read_message(self):
    """
    Pulls from the control socket until we either have a complete message or
    encounter a problem.
    
    Returns:
      ControlMessage read from the socket
    """
    
    lines, raw_content = [], ""
    
    while True:
      line = self._control_socket_file.readline()
      raw_content += line
      
      # Tor control lines are of the form...
      # <status code><divider><content>\r\n
      #
      # status code - Three character code for the type of response (defined in
      #     section 4 of the control-spec).
      # divider - Single character to indicate if this is mid-reply, data, or
      #     an end to the message (defined in section 2.3 of the control-spec).
      # content - The following content is the actual payload of the line.
      
      if len(line) < 4:
        raise ProtocolError("Badly formatted reply line: too short")
      elif not line.endswith("\r\n"):
        raise ProtocolError("All lines should end with CRLF")
      
      line = line[:-2] # strips off the CRLF
      status_code, divider, content = line[:3], line[3], line[4:]
      
      if divider == "-":
        # mid-reply line, keep pulling for more content
        lines.append((status_code, divider, content))
      elif divider == " ":
        # end of the message, return the message
        lines.append((status_code, divider, content))
        return ControlMessage(lines, raw_content)
      elif divider == "+":
        # data entry, all of the following lines belong to the content until we
        # get a line with just a period
        
        while True:
          line = self._control_socket_file.readline()
          raw_content += line
          
          if not line.endswith("\r\n"):
            raise ProtocolError("All lines should end with CRLF")
          elif line == ".\r\n":
            break # data block termination
          
          line = line[:-2] # strips off the CRLF
          
          # lines starting with a pariod are escaped by a second period (as per
          # section 2.4 of the control-spec)
          if line.startswith(".."): line = line[1:]
          
          # appends to previous content, using a newline rather than CRLF
          # separator (more contentional for multi-line string content outside
          # the windows world)
          
          content += "\n" + line
        
        lines.append((status_code, divider, content))
      else:
        raise ProtocolError("Unrecognized type '%s': %s" % (divider, line))
  
  def close(self):
    """
    Terminates the control connection.
    """
    
    self._is_running = False
    
    # if we haven't yet established a connection then this raises an error
    # socket.error: [Errno 107] Transport endpoint is not connected
    try: self._control_socket.shutdown(socket.SHUT_RDWR)
    except socket.error: pass
    
    self._control_socket.close()
    self._control_socket_file.close()
    
    # wake up the event thread so it can terminate
    self._event_cond.acquire()
    self._event_cond.notifyAll()
    self._event_cond.release()
    
    self._event_thread.join()
    self._reader_thread.join()

# temporary function for getting a connection
def test_connection():
  s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
  s.connect(("127.0.0.1", 9051))
  return ControlConnection(s)

