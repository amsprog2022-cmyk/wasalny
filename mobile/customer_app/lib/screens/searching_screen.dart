import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:google_fonts/google_fonts.dart';

import '../config/theme.dart';
import '../state/active_ride_provider.dart';
import 'home_screen.dart';
import 'trip_in_progress_screen.dart';

/// Radar pulse animation while backend broadcasts to captains.
class SearchingScreen extends ConsumerStatefulWidget {
  const SearchingScreen({super.key});

  @override
  ConsumerState<SearchingScreen> createState() => _SearchingScreenState();
}

class _SearchingScreenState extends ConsumerState<SearchingScreen>
    with SingleTickerProviderStateMixin {
  late final AnimationController _pulse;

  @override
  void initState() {
    super.initState();
    _pulse = AnimationController(
      vsync: this,
      duration: const Duration(seconds: 2),
    )..repeat();
  }

  @override
  void dispose() {
    _pulse.dispose();
    super.dispose();
  }

  Future<void> _cancel() async {
    final ride = ref.read(activeRideProvider).ride;
    if (ride == null) return;
    final confirm = await showDialog<bool>(
      context: context,
      builder: (_) => AlertDialog(
        backgroundColor: AppColors.surface,
        title: Text('تلغي الطلب؟', style: GoogleFonts.cairo()),
        content: Text(
          'متأكد إنك عايز تلغي الرحلة؟',
          style: GoogleFonts.cairo(color: AppColors.textMuted),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context, false),
            child: Text('لا', style: GoogleFonts.cairo(color: AppColors.textMuted)),
          ),
          TextButton(
            onPressed: () => Navigator.pop(context, true),
            child: Text('نعم، ألغي', style: GoogleFonts.cairo(color: AppColors.statusCancelled)),
          ),
        ],
      ),
    );
    if (confirm != true) return;
    await ref.read(activeRideProvider.notifier).cancel(ride.id, reason: 'customer_cancelled');
    if (!mounted) return;
    ref.read(activeRideProvider.notifier).clear();
    Navigator.of(context).pushAndRemoveUntil(
      MaterialPageRoute(builder: (_) => const HomeScreen()),
      (_) => false,
    );
  }

  @override
  Widget build(BuildContext context) {
    ref.listen(activeRideProvider, (prev, next) {
      if (!next.hasRide) return;
      final status = next.ride!.status;
      if (status == 'assigned' || status == 'started') {
        Navigator.of(context).pushReplacement(
          MaterialPageRoute(builder: (_) => const TripInProgressScreen()),
        );
      } else if (next.isTerminal) {
        _handleTerminal(next.ride!.status);
      }
    });

    return Scaffold(
      body: SafeArea(
        child: Padding(
          padding: const EdgeInsets.all(24),
          child: Column(
            children: [
              const SizedBox(height: 40),
              SizedBox(
                width: 200,
                height: 200,
                child: AnimatedBuilder(
                  animation: _pulse,
                  builder: (_, __) => _RadarPulse(t: _pulse.value),
                ),
              ),
              const SizedBox(height: 32),
              Text(
                'بندور على أقرب كابتن…',
                style: GoogleFonts.cairo(
                  fontSize: 22,
                  fontWeight: FontWeight.w900,
                ),
              ),
              const SizedBox(height: 8),
              Text(
                'ثواني وهنلاقيلك واحد قريب.',
                textAlign: TextAlign.center,
                style: GoogleFonts.cairo(color: AppColors.textMuted),
              ),
              const Spacer(),
              OutlinedButton(
                onPressed: _cancel,
                child: const Text('ألغي الطلب'),
              ),
              const SizedBox(height: 8),
            ],
          ),
        ),
      ),
    );
  }

  void _handleTerminal(String status) {
    String msg = 'الرحلة اتلغت';
    if (status == 'cancelled') {
      final reason = ref.read(activeRideProvider).ride?.cancelReason;
      if (reason == 'no_driver_available') {
        msg = 'معلش، مفيش كباتن متاحين دلوقتي. جرب تاني بعد شوية.';
      }
    }
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(content: Text(msg, style: GoogleFonts.cairo())),
    );
    ref.read(activeRideProvider.notifier).clear();
    Navigator.of(context).pushAndRemoveUntil(
      MaterialPageRoute(builder: (_) => const HomeScreen()),
      (_) => false,
    );
  }
}

class _RadarPulse extends StatelessWidget {
  final double t;
  const _RadarPulse({required this.t});

  @override
  Widget build(BuildContext context) {
    return Stack(
      alignment: Alignment.center,
      children: [
        for (int i = 0; i < 3; i++) _pulse(i),
        Container(
          width: 80,
          height: 80,
          decoration: const BoxDecoration(
            color: AppColors.primary,
            shape: BoxShape.circle,
          ),
          child: const Icon(Icons.local_taxi, color: Colors.white, size: 36),
        ),
      ],
    );
  }

  Widget _pulse(int idx) {
    final phase = (t + idx * 0.33) % 1;
    final size = 80 + phase * 120;
    final opacity = (1 - phase) * 0.4;
    return Container(
      width: size,
      height: size,
      decoration: BoxDecoration(
        shape: BoxShape.circle,
        color: AppColors.primary.withValues(alpha: opacity),
      ),
    );
  }
}
