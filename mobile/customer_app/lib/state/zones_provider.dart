import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../models/zone.dart';
import '../services/rides_service.dart';

final ridesServiceProvider = Provider<RidesService>((ref) => RidesService());

final zonesProvider = FutureProvider<List<Zone>>((ref) async {
  final raw = await ref.read(ridesServiceProvider).listZones();
  return raw
      .map((e) => Zone.fromJson(e as Map<String, dynamic>))
      .where((z) => z.isActive)
      .toList();
});
