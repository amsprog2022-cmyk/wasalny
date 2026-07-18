import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:google_fonts/google_fonts.dart';

import '../config/theme.dart';
import '../models/zone.dart';
import '../state/active_ride_provider.dart';
import '../state/auth_provider.dart';
import '../widgets/primary_button.dart';
import 'history_screen.dart';
import 'profile_screen.dart';
import 'quote_screen.dart';
import 'searching_screen.dart';
import 'trip_in_progress_screen.dart';
import 'zone_picker_screen.dart';

class HomeScreen extends ConsumerStatefulWidget {
  const HomeScreen({super.key});

  @override
  ConsumerState<HomeScreen> createState() => _HomeScreenState();
}

class _HomeScreenState extends ConsumerState<HomeScreen> {
  Zone? _pickup;
  Zone? _dropoff;

  Future<void> _pickPickup() async {
    final selected = await Navigator.of(context).push<Zone>(
      MaterialPageRoute(
        builder: (_) => ZonePickerScreen(
          title: 'اختار نقطة الانطلاق',
          excludeZone: _dropoff,
        ),
      ),
    );
    if (selected != null) setState(() => _pickup = selected);
  }

  Future<void> _pickDropoff() async {
    final selected = await Navigator.of(context).push<Zone>(
      MaterialPageRoute(
        builder: (_) => ZonePickerScreen(
          title: 'اختار الوجهة',
          excludeZone: _pickup,
        ),
      ),
    );
    if (selected != null) setState(() => _dropoff = selected);
  }

  void _continue() {
    if (_pickup == null || _dropoff == null) return;
    Navigator.of(context).push(
      MaterialPageRoute(
        builder: (_) => QuoteScreen(pickup: _pickup!, dropoff: _dropoff!),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final auth = ref.watch(authProvider);
    final active = ref.watch(activeRideProvider);

    // Auto-navigate if there's an active ride
    ref.listen(activeRideProvider, (prev, next) {
      if (next.hasRide && !next.isTerminal) {
        final status = next.ride!.status;
        if (status == 'broadcasting' || status == 'new') {
          Navigator.of(context).push(
            MaterialPageRoute(builder: (_) => const SearchingScreen()),
          );
        } else if (status == 'assigned' || status == 'started') {
          Navigator.of(context).push(
            MaterialPageRoute(builder: (_) => const TripInProgressScreen()),
          );
        }
      }
    });

    return Scaffold(
      body: SafeArea(
        child: SingleChildScrollView(
          padding: const EdgeInsets.all(20),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              _header(auth.customer?.name ?? 'ضيف'),
              const SizedBox(height: 24),

              if (active.hasRide && !active.isTerminal)
                _activeRideBanner(active)
              else ...[
                Text(
                  'عايز تروح فين؟',
                  style: GoogleFonts.cairo(
                    fontSize: 22,
                    fontWeight: FontWeight.w900,
                  ),
                ),
                const SizedBox(height: 20),
                _locationCard(
                  label: 'من فين',
                  icon: Icons.my_location,
                  iconColor: AppColors.primary,
                  zone: _pickup,
                  placeholder: 'اختار حي الانطلاق',
                  onTap: _pickPickup,
                ),
                const SizedBox(height: 12),
                _locationCard(
                  label: 'لفين',
                  icon: Icons.place,
                  iconColor: AppColors.success,
                  zone: _dropoff,
                  placeholder: 'اختار الوجهة',
                  onTap: _pickDropoff,
                ),
                const SizedBox(height: 24),
                PrimaryButton(
                  label: 'شوف السعر',
                  icon: Icons.arrow_forward,
                  onPressed: (_pickup == null || _dropoff == null) ? null : _continue,
                ),
              ],

              const SizedBox(height: 32),
              _quickActions(),
            ],
          ),
        ),
      ),
    );
  }

  Widget _header(String name) {
    return Row(
      children: [
        CircleAvatar(
          backgroundColor: AppColors.surface,
          child: Text(
            name.characters.firstOrNull ?? '؟',
            style: GoogleFonts.cairo(
              color: AppColors.primary,
              fontWeight: FontWeight.w900,
            ),
          ),
        ),
        const SizedBox(width: 12),
        Expanded(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(
                'أهلاً يا $name',
                style: GoogleFonts.cairo(fontSize: 12, color: AppColors.textMuted),
              ),
              Text(
                'وصلني بنها',
                style: GoogleFonts.cairo(
                  fontSize: 20,
                  fontWeight: FontWeight.w900,
                  color: AppColors.primary,
                ),
              ),
            ],
          ),
        ),
        IconButton(
          onPressed: () => Navigator.of(context).push(
            MaterialPageRoute(builder: (_) => const ProfileScreen()),
          ),
          icon: const Icon(Icons.person_outline),
        ),
      ],
    );
  }

  Widget _locationCard({
    required String label,
    required IconData icon,
    required Color iconColor,
    required Zone? zone,
    required String placeholder,
    required VoidCallback onTap,
  }) {
    return Card(
      child: InkWell(
        borderRadius: BorderRadius.circular(AppRadii.card),
        onTap: onTap,
        child: Padding(
          padding: const EdgeInsets.all(16),
          child: Row(
            children: [
              Icon(icon, color: iconColor),
              const SizedBox(width: 16),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      label,
                      style: GoogleFonts.cairo(fontSize: 12, color: AppColors.textMuted),
                    ),
                    const SizedBox(height: 2),
                    Text(
                      zone?.nameAr ?? placeholder,
                      style: GoogleFonts.cairo(
                        fontSize: 16,
                        fontWeight: FontWeight.w600,
                        color: zone == null ? AppColors.textMuted : Colors.white,
                      ),
                    ),
                  ],
                ),
              ),
              const Icon(Icons.chevron_left, color: AppColors.textMuted),
            ],
          ),
        ),
      ),
    );
  }

  Widget _activeRideBanner(ActiveRideState state) {
    final r = state.ride!;
    return Card(
      color: AppColors.primary,
      child: InkWell(
        borderRadius: BorderRadius.circular(AppRadii.card),
        onTap: () => Navigator.of(context).push(
          MaterialPageRoute(
            builder: (_) => r.driver != null
                ? const TripInProgressScreen()
                : const SearchingScreen(),
          ),
        ),
        child: Padding(
          padding: const EdgeInsets.all(16),
          child: Row(
            children: [
              const Icon(Icons.local_taxi, color: Colors.white, size: 32),
              const SizedBox(width: 16),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      'رحلتك شغالة',
                      style: GoogleFonts.cairo(
                        color: Colors.white,
                        fontWeight: FontWeight.w900,
                        fontSize: 16,
                      ),
                    ),
                    Text(
                      '${r.fromZoneAr ?? "?"} ← ${r.toZoneAr ?? "?"}',
                      style: GoogleFonts.cairo(
                        color: Colors.white.withValues(alpha: 0.85),
                      ),
                    ),
                  ],
                ),
              ),
              const Icon(Icons.chevron_left, color: Colors.white),
            ],
          ),
        ),
      ),
    );
  }

  Widget _quickActions() {
    return Row(
      children: [
        Expanded(
          child: _quickTile(
            icon: Icons.history,
            label: 'رحلاتي',
            onTap: () => Navigator.of(context).push(
              MaterialPageRoute(builder: (_) => const HistoryScreen()),
            ),
          ),
        ),
        const SizedBox(width: 12),
        Expanded(
          child: _quickTile(
            icon: Icons.person_outline,
            label: 'حسابي',
            onTap: () => Navigator.of(context).push(
              MaterialPageRoute(builder: (_) => const ProfileScreen()),
            ),
          ),
        ),
      ],
    );
  }

  Widget _quickTile({
    required IconData icon,
    required String label,
    required VoidCallback onTap,
  }) {
    return Card(
      child: InkWell(
        borderRadius: BorderRadius.circular(AppRadii.card),
        onTap: onTap,
        child: Padding(
          padding: const EdgeInsets.symmetric(vertical: 20),
          child: Column(
            children: [
              Icon(icon, color: AppColors.primary, size: 28),
              const SizedBox(height: 8),
              Text(
                label,
                style: GoogleFonts.cairo(fontWeight: FontWeight.w600),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
