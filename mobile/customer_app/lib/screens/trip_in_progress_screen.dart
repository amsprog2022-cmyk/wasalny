import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:google_fonts/google_fonts.dart';

import '../config/theme.dart';
import '../state/active_ride_provider.dart';
import 'home_screen.dart';
import 'rating_screen.dart';

class TripInProgressScreen extends ConsumerStatefulWidget {
  const TripInProgressScreen({super.key});

  @override
  ConsumerState<TripInProgressScreen> createState() => _TripInProgressScreenState();
}

class _TripInProgressScreenState extends ConsumerState<TripInProgressScreen> {
  Future<void> _sos() async {
    final ride = ref.read(activeRideProvider).ride;
    if (ride == null) return;
    final confirm = await showDialog<bool>(
      context: context,
      builder: (_) => AlertDialog(
        backgroundColor: AppColors.surface,
        title: Text('طلب نجدة', style: GoogleFonts.cairo(color: AppColors.statusCancelled)),
        content: Text(
          'هنبعت تنبيه فوري للفريق، وهيكلموك على طول. اضغط تأكيد لو محتاج مساعدة.',
          style: GoogleFonts.cairo(color: AppColors.textMuted),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context, false),
            child: Text('لا', style: GoogleFonts.cairo(color: AppColors.textMuted)),
          ),
          TextButton(
            onPressed: () => Navigator.pop(context, true),
            child: Text('تأكيد', style: GoogleFonts.cairo(color: AppColors.statusCancelled)),
          ),
        ],
      ),
    );
    if (confirm != true) return;
    await ref.read(activeRideProvider.notifier).sos(ride.id, message: 'طلب من التطبيق');
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(content: Text('تم إرسال طلب النجدة. هيكلموك حالاً.', style: GoogleFonts.cairo())),
    );
  }

  @override
  Widget build(BuildContext context) {
    final state = ref.watch(activeRideProvider);
    final ride = state.ride;

    ref.listen(activeRideProvider, (prev, next) {
      if (next.hasRide && next.ride!.status == 'completed') {
        Navigator.of(context).pushReplacement(
          MaterialPageRoute(builder: (_) => RatingScreen(ride: next.ride!)),
        );
      } else if (next.hasRide && next.isTerminal && next.ride!.status != 'completed') {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text('اتلغت الرحلة', style: GoogleFonts.cairo())),
        );
        ref.read(activeRideProvider.notifier).clear();
        Navigator.of(context).pushAndRemoveUntil(
          MaterialPageRoute(builder: (_) => const HomeScreen()),
          (_) => false,
        );
      }
    });

    if (ride == null) {
      return const Scaffold(body: Center(child: CircularProgressIndicator()));
    }

    final statusText = _statusLabel(ride.status);
    return Scaffold(
      body: SafeArea(
        child: Column(
          children: [
            Padding(
              padding: const EdgeInsets.all(20),
              child: Row(
                children: [
                  Expanded(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text('حالة الرحلة', style: GoogleFonts.cairo(color: AppColors.textMuted, fontSize: 12)),
                        Text(
                          statusText,
                          style: GoogleFonts.cairo(fontSize: 22, fontWeight: FontWeight.w900),
                        ),
                      ],
                    ),
                  ),
                  IconButton.filled(
                    onPressed: _sos,
                    style: IconButton.styleFrom(
                      backgroundColor: AppColors.statusCancelled,
                    ),
                    icon: const Icon(Icons.sos, color: Colors.white),
                  ),
                ],
              ),
            ),
            Expanded(
              child: SingleChildScrollView(
                padding: const EdgeInsets.symmetric(horizontal: 20),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.stretch,
                  children: [
                    if (ride.driver != null) _driverCard(ride.driver!, ride.priceEgp),
                    const SizedBox(height: 16),
                    _routeCard(ride.fromZoneAr, ride.toZoneAr),
                    const SizedBox(height: 16),
                    _priceCard(ride.priceEgp, ride.noShowFeeEgp),
                  ],
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }

  Widget _driverCard(dynamic driver, double price) {
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Row(
          children: [
            const CircleAvatar(
              radius: 28,
              backgroundColor: AppColors.surfaceRaised,
              child: Icon(Icons.person, color: AppColors.primary, size: 32),
            ),
            const SizedBox(width: 16),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    driver.name as String,
                    style: GoogleFonts.cairo(fontSize: 18, fontWeight: FontWeight.w900),
                  ),
                  if (driver.carModel != null)
                    Text(
                      '${driver.carModel} · ${driver.carPlate ?? ''}',
                      style: GoogleFonts.cairo(color: AppColors.textMuted, fontSize: 13),
                    ),
                  const SizedBox(height: 4),
                  Row(
                    children: [
                      const Icon(Icons.star, color: AppColors.warning, size: 16),
                      const SizedBox(width: 4),
                      Text(
                        (driver.rating as double?)?.toStringAsFixed(1) ?? '—',
                        style: GoogleFonts.cairo(fontWeight: FontWeight.w600),
                      ),
                    ],
                  ),
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }

  Widget _routeCard(String? from, String? to) {
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          children: [
            Row(children: [
              const Icon(Icons.my_location, color: AppColors.primary),
              const SizedBox(width: 12),
              Expanded(child: Text(from ?? '—', style: GoogleFonts.cairo(fontSize: 15))),
            ]),
            const SizedBox(height: 8),
            Row(children: [
              const Icon(Icons.place, color: AppColors.success),
              const SizedBox(width: 12),
              Expanded(child: Text(to ?? '—', style: GoogleFonts.cairo(fontSize: 15))),
            ]),
          ],
        ),
      ),
    );
  }

  Widget _priceCard(double price, double noShowFee) {
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Row(
          mainAxisAlignment: MainAxisAlignment.spaceBetween,
          children: [
            Text('اللي هتدفعه', style: GoogleFonts.cairo(fontSize: 15)),
            Text(
              '${(price + noShowFee).toStringAsFixed(0)} ج.م',
              style: GoogleFonts.cairo(
                fontSize: 22,
                fontWeight: FontWeight.w900,
                color: AppColors.primary,
              ),
            ),
          ],
        ),
      ),
    );
  }

  String _statusLabel(String status) {
    switch (status) {
      case 'assigned':
        return 'الكابتن جاي';
      case 'started':
        return 'في الطريق';
      case 'completed':
        return 'خلصت';
      default:
        return status;
    }
  }
}
