import 'dart:async';

import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../models/ride.dart';
import '../services/socket_service.dart';
import 'zones_provider.dart';

/// Live state of the customer's currently active ride (if any).
///
/// Updated from three sources:
/// 1. Explicit fetch via `refresh(rideId)`
/// 2. Socket.IO events on the /customer namespace
/// 3. `set()` after a successful `createRide()`
class ActiveRideState {
  final Ride? ride;
  final bool isSearching;

  const ActiveRideState({this.ride, this.isSearching = false});

  bool get hasRide => ride != null;
  bool get isTerminal => ride?.isTerminal ?? false;

  ActiveRideState copyWith({Ride? ride, bool? isSearching, bool clearRide = false}) {
    return ActiveRideState(
      ride: clearRide ? null : (ride ?? this.ride),
      isSearching: isSearching ?? this.isSearching,
    );
  }
}

class ActiveRideNotifier extends StateNotifier<ActiveRideState> {
  ActiveRideNotifier(this.ref) : super(const ActiveRideState()) {
    _listen();
  }

  final Ref ref;
  StreamSubscription<SocketEvent>? _subscription;

  void _listen() {
    _subscription = CustomerSocket.instance.events.listen((event) {
      switch (event.name) {
        case 'broadcast_started':
          state = state.copyWith(isSearching: true);
          _mergeRideFromEvent(event.data);
          break;
        case 'trip_assigned':
          _mergeRideFromEvent(event.data);
          state = state.copyWith(isSearching: false);
          break;
        case 'trip_status_changed':
          _mergeRideFromEvent(event.data);
          break;
        case 'trip_cancelled':
          _mergeRideFromEvent(event.data);
          state = state.copyWith(isSearching: false);
          break;
      }
    });
  }

  void _mergeRideFromEvent(Map<String, dynamic> data) {
    final rideJson = data['ride'];
    if (rideJson is Map<String, dynamic>) {
      state = state.copyWith(ride: Ride.fromJson(rideJson));
    }
  }

  void setRide(Ride ride) {
    state = ActiveRideState(
      ride: ride,
      isSearching: ride.status == 'broadcasting' || ride.status == 'new',
    );
  }

  Future<void> refresh(int rideId) async {
    final r = await ref.read(ridesServiceProvider).getRide(rideId);
    state = state.copyWith(ride: r, isSearching: r.status == 'broadcasting');
  }

  Future<void> cancel(int rideId, {String? reason}) async {
    final r = await ref.read(ridesServiceProvider).cancelRide(rideId, reason: reason);
    state = state.copyWith(ride: r, isSearching: false);
  }

  Future<void> sos(int rideId, {String? message}) async {
    await ref.read(ridesServiceProvider).sos(rideId, message: message);
  }

  void clear() {
    state = const ActiveRideState();
  }

  @override
  void dispose() {
    _subscription?.cancel();
    super.dispose();
  }
}

final activeRideProvider =
    StateNotifierProvider<ActiveRideNotifier, ActiveRideState>(
  (ref) => ActiveRideNotifier(ref),
);
