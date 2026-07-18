import 'dart:async';

import 'package:socket_io_client/socket_io_client.dart' as io;

import '../config/app_config.dart';
import 'api_client.dart';

/// Wraps the Socket.IO client for the customer namespace.
///
/// Events (server → us):
///   trip_status_changed
///   trip_assigned
///   trip_cancelled
///   broadcast_started
class CustomerSocket {
  CustomerSocket._();
  static final instance = CustomerSocket._();

  io.Socket? _socket;
  final _events = StreamController<SocketEvent>.broadcast();

  Stream<SocketEvent> get events => _events.stream;

  Future<void> connect() async {
    final token = await ApiClient.instance.getToken();
    if (token == null || token.isEmpty) return;

    _socket?.dispose();
    _socket = io.io(
      '${AppConfig.socketUrl}/customer',
      io.OptionBuilder()
          .setTransports(['websocket'])
          .setQuery({'token': token})
          .disableAutoConnect()
          .build(),
    );

    _socket!
      ..onConnect((_) => _events.add(SocketEvent('__connected__', const {})))
      ..onDisconnect((_) => _events.add(SocketEvent('__disconnected__', const {})))
      ..on('customer:connected', (data) => _events.add(SocketEvent('customer:connected', _asMap(data))))
      ..on('trip_status_changed', (data) => _events.add(SocketEvent('trip_status_changed', _asMap(data))))
      ..on('trip_assigned', (data) => _events.add(SocketEvent('trip_assigned', _asMap(data))))
      ..on('trip_cancelled', (data) => _events.add(SocketEvent('trip_cancelled', _asMap(data))))
      ..on('broadcast_started', (data) => _events.add(SocketEvent('broadcast_started', _asMap(data))));

    _socket!.connect();
  }

  Map<String, dynamic> _asMap(dynamic data) {
    if (data is Map<String, dynamic>) return data;
    if (data is Map) return Map<String, dynamic>.from(data);
    return const {};
  }

  void disconnect() {
    _socket?.dispose();
    _socket = null;
  }
}

class SocketEvent {
  final String name;
  final Map<String, dynamic> data;
  SocketEvent(this.name, this.data);
}
