import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:google_fonts/google_fonts.dart';

import '../config/theme.dart';
import '../models/ride.dart';
import '../state/active_ride_provider.dart';
import '../state/zones_provider.dart';
import '../widgets/primary_button.dart';
import 'home_screen.dart';

class RatingScreen extends ConsumerStatefulWidget {
  final Ride ride;
  const RatingScreen({super.key, required this.ride});

  @override
  ConsumerState<RatingScreen> createState() => _RatingScreenState();
}

class _RatingScreenState extends ConsumerState<RatingScreen> {
  int _stars = 0;
  final _commentCtrl = TextEditingController();
  bool _submitting = false;

  Future<void> _submit() async {
    if (_stars == 0) return;
    setState(() => _submitting = true);
    try {
      await ref.read(ridesServiceProvider).rateRide(
            widget.ride.id,
            _stars,
            comment: _commentCtrl.text.trim().isEmpty ? null : _commentCtrl.text.trim(),
          );
    } catch (_) {
      // fail silently; rating is best-effort
    }
    if (!mounted) return;
    ref.read(activeRideProvider.notifier).clear();
    Navigator.of(context).pushAndRemoveUntil(
      MaterialPageRoute(builder: (_) => const HomeScreen()),
      (_) => false,
    );
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: SafeArea(
        child: Padding(
          padding: const EdgeInsets.all(24),
          child: Column(
            children: [
              const SizedBox(height: 40),
              const Text('✅', style: TextStyle(fontSize: 80)),
              const SizedBox(height: 16),
              Text(
                'وصلت بأمان!',
                style: GoogleFonts.cairo(fontSize: 26, fontWeight: FontWeight.w900),
              ),
              const SizedBox(height: 8),
              Text(
                'قيّم رحلتك مع ${widget.ride.driver?.name ?? "الكابتن"}',
                style: GoogleFonts.cairo(color: AppColors.textMuted),
              ),
              const SizedBox(height: 32),
              Row(
                mainAxisAlignment: MainAxisAlignment.center,
                children: List.generate(5, (i) {
                  final filled = i < _stars;
                  return IconButton(
                    iconSize: 44,
                    onPressed: () => setState(() => _stars = i + 1),
                    icon: Icon(
                      filled ? Icons.star : Icons.star_border,
                      color: AppColors.warning,
                    ),
                  );
                }),
              ),
              const SizedBox(height: 24),
              TextField(
                controller: _commentCtrl,
                maxLines: 3,
                decoration: const InputDecoration(
                  hintText: 'اكتب تعليقك (اختياري)',
                ),
              ),
              const Spacer(),
              PrimaryButton(
                label: 'أرسل',
                loading: _submitting,
                onPressed: _stars == 0 ? null : _submit,
              ),
              const SizedBox(height: 8),
              TextButton(
                onPressed: () {
                  ref.read(activeRideProvider.notifier).clear();
                  Navigator.of(context).pushAndRemoveUntil(
                    MaterialPageRoute(builder: (_) => const HomeScreen()),
                    (_) => false,
                  );
                },
                child: Text('تخطي', style: GoogleFonts.cairo(color: AppColors.textMuted)),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
