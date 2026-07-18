import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:google_fonts/google_fonts.dart';

import '../config/theme.dart';
import '../models/zone.dart';
import '../state/zones_provider.dart';

class ZonePickerScreen extends ConsumerStatefulWidget {
  final String title;
  final Zone? excludeZone;

  const ZonePickerScreen({super.key, required this.title, this.excludeZone});

  @override
  ConsumerState<ZonePickerScreen> createState() => _ZonePickerScreenState();
}

class _ZonePickerScreenState extends ConsumerState<ZonePickerScreen> {
  String _query = '';

  @override
  Widget build(BuildContext context) {
    final zonesAsync = ref.watch(zonesProvider);
    return Scaffold(
      appBar: AppBar(title: Text(widget.title)),
      body: SafeArea(
        child: Column(
          children: [
            Padding(
              padding: const EdgeInsets.all(16),
              child: TextField(
                autofocus: false,
                onChanged: (v) => setState(() => _query = v.trim()),
                decoration: InputDecoration(
                  hintText: 'ابحث عن حي…',
                  prefixIcon: const Icon(Icons.search, color: AppColors.textMuted),
                ),
              ),
            ),
            Expanded(
              child: zonesAsync.when(
                loading: () => const Center(child: CircularProgressIndicator()),
                error: (e, _) => Center(
                  child: Padding(
                    padding: const EdgeInsets.all(24),
                    child: Text(
                      'حصل خطأ في تحميل المناطق: $e',
                      textAlign: TextAlign.center,
                      style: GoogleFonts.cairo(color: AppColors.textMuted),
                    ),
                  ),
                ),
                data: (zones) {
                  final filtered = zones.where((z) {
                    if (widget.excludeZone != null && z.id == widget.excludeZone!.id) return false;
                    if (_query.isEmpty) return true;
                    return z.nameAr.contains(_query) ||
                        z.nameEn.toLowerCase().contains(_query.toLowerCase()) ||
                        z.slug.toLowerCase().contains(_query.toLowerCase());
                  }).toList();

                  if (filtered.isEmpty) {
                    return Center(
                      child: Text(
                        'مفيش نتيجة',
                        style: GoogleFonts.cairo(color: AppColors.textMuted),
                      ),
                    );
                  }

                  return ListView.separated(
                    padding: const EdgeInsets.symmetric(horizontal: 16),
                    itemCount: filtered.length,
                    separatorBuilder: (_, __) => const SizedBox(height: 8),
                    itemBuilder: (_, i) {
                      final z = filtered[i];
                      return _ZoneTile(zone: z, onTap: () => Navigator.of(context).pop(z));
                    },
                  );
                },
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _ZoneTile extends StatelessWidget {
  final Zone zone;
  final VoidCallback onTap;
  const _ZoneTile({required this.zone, required this.onTap});

  @override
  Widget build(BuildContext context) {
    return InkWell(
      borderRadius: BorderRadius.circular(AppRadii.card),
      onTap: onTap,
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 14),
        decoration: BoxDecoration(
          color: AppColors.surface,
          borderRadius: BorderRadius.circular(AppRadii.card),
        ),
        child: Row(
          children: [
            const CircleAvatar(
              backgroundColor: AppColors.surfaceRaised,
              child: Icon(Icons.place, color: AppColors.primary),
            ),
            const SizedBox(width: 12),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    zone.nameAr,
                    style: GoogleFonts.cairo(
                      fontSize: 16,
                      fontWeight: FontWeight.w600,
                    ),
                  ),
                  Text(
                    zone.nameEn,
                    style: GoogleFonts.cairo(
                      fontSize: 12,
                      color: AppColors.textMuted,
                    ),
                  ),
                ],
              ),
            ),
            const Icon(Icons.chevron_left, color: AppColors.textMuted),
          ],
        ),
      ),
    );
  }
}
