import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../models/ride.dart';
import 'zones_provider.dart';

final rideHistoryProvider = FutureProvider<List<Ride>>((ref) async {
  return ref.read(ridesServiceProvider).myRides();
});
