import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:google_fonts/google_fonts.dart';

import '../config/theme.dart';
import '../models/ride.dart';
import '../state/history_provider.dart';

class HistoryScreen extends ConsumerWidget {
  const HistoryScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final async = ref.watch(rideHistoryProvider);
    return Scaffold(
      appBar: AppBar(title: const Text('رحلاتي')),
      body: RefreshIndicator(
        onRefresh: () async => ref.refresh(rideHistoryProvider.future),
        child: async.when(
          loading: () => const Center(child: CircularProgressIndicator()),
          error: (e, _) => ListView(
            children: [
              Padding(
                padding: const EdgeInsets.all(24),
                child: Text(
                  'حصل خطأ في التحميل: $e',
                  textAlign: TextAlign.center,
                  style: GoogleFonts.cairo(color: AppColors.textMuted),
                ),
              ),
            ],
          ),
          data: (rides) {
            if (rides.isEmpty) {
              return ListView(
                children: [
                  const SizedBox(height: 80),
                  const Center(child: Text('🚗', style: TextStyle(fontSize: 60))),
                  const SizedBox(height: 16),
                  Center(
                    child: Text(
                      'مفيش رحلات لسه',
                      style: GoogleFonts.cairo(fontSize: 18, fontWeight: FontWeight.w700),
                    ),
                  ),
                  const SizedBox(height: 8),
                  Center(
                    child: Text(
                      'أول رحلة هتحس بيها فرق',
                      style: GoogleFonts.cairo(color: AppColors.textMuted),
                    ),
                  ),
                ],
              );
            }
            return ListView.separated(
              padding: const EdgeInsets.all(16),
              itemCount: rides.length,
              separatorBuilder: (_, __) => const SizedBox(height: 12),
              itemBuilder: (_, i) => _RideCard(ride: rides[i]),
            );
          },
        ),
      ),
    );
  }
}

class _RideCard extends StatelessWidget {
  final Ride ride;
  const _RideCard({required this.ride});

  @override
  Widget build(BuildContext context) {
    final statusColor = switch (ride.status) {
      'completed' => AppColors.success,
      'cancelled' || 'cancelled_no_show' => AppColors.statusCancelled,
      _ => AppColors.textMuted,
    };
    final statusText = switch (ride.status) {
      'completed' => 'خلصت',
      'cancelled' => 'اتلغت',
      'cancelled_no_show' => 'ماحضرتش',
      _ => ride.status,
    };
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Expanded(
                  child: Text(
                    '${ride.fromZoneAr ?? "?"} ← ${ride.toZoneAr ?? "?"}',
                    style: GoogleFonts.cairo(fontSize: 15, fontWeight: FontWeight.w600),
                  ),
                ),
                Text(
                  statusText,
                  style: GoogleFonts.cairo(color: statusColor, fontWeight: FontWeight.w700),
                ),
              ],
            ),
            const SizedBox(height: 4),
            Text(
              _formatDate(ride.createdAt),
              style: GoogleFonts.cairo(color: AppColors.textMuted, fontSize: 12),
            ),
            const SizedBox(height: 12),
            Row(
              children: [
                if (ride.rating != null) ...[
                  const Icon(Icons.star, size: 14, color: AppColors.warning),
                  const SizedBox(width: 4),
                  Text('${ride.rating}', style: GoogleFonts.cairo(fontSize: 12)),
                  const SizedBox(width: 12),
                ],
                const Spacer(),
                Text(
                  '${ride.priceEgp.toStringAsFixed(0)} ج.م',
                  style: GoogleFonts.cairo(
                    fontSize: 16,
                    fontWeight: FontWeight.w900,
                    color: AppColors.primary,
                  ),
                ),
              ],
            ),
          ],
        ),
      ),
    );
  }

  String _formatDate(DateTime? dt) {
    if (dt == null) return '—';
    final local = dt.toLocal();
    return '${local.day}/${local.month} · ${local.hour.toString().padLeft(2, '0')}:${local.minute.toString().padLeft(2, '0')}';
  }
}
