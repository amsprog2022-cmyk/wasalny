import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:google_fonts/google_fonts.dart';

import '../config/theme.dart';
import '../models/quote.dart';
import '../models/zone.dart';
import '../state/active_ride_provider.dart';
import '../state/zones_provider.dart';
import '../widgets/primary_button.dart';
import 'searching_screen.dart';

class QuoteScreen extends ConsumerStatefulWidget {
  final Zone pickup;
  final Zone dropoff;
  const QuoteScreen({super.key, required this.pickup, required this.dropoff});

  @override
  ConsumerState<QuoteScreen> createState() => _QuoteScreenState();
}

class _QuoteScreenState extends ConsumerState<QuoteScreen> {
  Quote? _quote;
  bool _loading = true;
  bool _booking = false;
  String? _error;

  @override
  void initState() {
    super.initState();
    _fetchQuote();
  }

  Future<void> _fetchQuote() async {
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final q = await ref.read(ridesServiceProvider).quote(
            widget.pickup.id,
            widget.dropoff.id,
          );
      if (!mounted) return;
      setState(() {
        _quote = q;
        _loading = false;
      });
    } catch (e) {
      if (!mounted) return;
      setState(() {
        _error = 'مش قادرين نجيب السعر: $e';
        _loading = false;
      });
    }
  }

  Future<void> _book() async {
    setState(() => _booking = true);
    try {
      final ride = await ref
          .read(ridesServiceProvider)
          .createRide(widget.pickup.id, widget.dropoff.id);
      ref.read(activeRideProvider.notifier).setRide(ride);
      if (!mounted) return;
      Navigator.of(context).pushReplacement(
        MaterialPageRoute(builder: (_) => const SearchingScreen()),
      );
    } catch (e) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text('مش قادرين نطلب الرحلة: $e', style: GoogleFonts.cairo())),
      );
      setState(() => _booking = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('تأكيد الرحلة')),
      body: SafeArea(
        child: Padding(
          padding: const EdgeInsets.all(20),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              _routeCard(),
              const SizedBox(height: 20),
              if (_loading)
                const Padding(
                  padding: EdgeInsets.symmetric(vertical: 40),
                  child: Center(child: CircularProgressIndicator()),
                )
              else if (_error != null)
                Card(
                  child: Padding(
                    padding: const EdgeInsets.all(16),
                    child: Column(
                      children: [
                        Text(_error!, style: GoogleFonts.cairo(color: AppColors.textMuted)),
                        const SizedBox(height: 12),
                        TextButton(
                          onPressed: _fetchQuote,
                          child: Text('حاول تاني', style: GoogleFonts.cairo(color: AppColors.primary)),
                        ),
                      ],
                    ),
                  ),
                )
              else
                _priceCard(_quote!),
              const Spacer(),
              PrimaryButton(
                label: 'يلا نطلب',
                loading: _booking,
                icon: Icons.local_taxi,
                onPressed: _quote == null ? null : _book,
              ),
              const SizedBox(height: 8),
              Text(
                'الدفع كاش بعد الوصول',
                textAlign: TextAlign.center,
                style: GoogleFonts.cairo(color: AppColors.textMuted, fontSize: 12),
              ),
            ],
          ),
        ),
      ),
    );
  }

  Widget _routeCard() {
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          children: [
            _routeRow(
              icon: Icons.my_location,
              iconColor: AppColors.primary,
              label: 'من',
              value: widget.pickup.nameAr,
            ),
            Padding(
              padding: const EdgeInsets.symmetric(vertical: 8),
              child: Row(
                children: [
                  const SizedBox(width: 20),
                  Container(width: 2, height: 20, color: Colors.white12),
                ],
              ),
            ),
            _routeRow(
              icon: Icons.place,
              iconColor: AppColors.success,
              label: 'إلى',
              value: widget.dropoff.nameAr,
            ),
          ],
        ),
      ),
    );
  }

  Widget _routeRow({
    required IconData icon,
    required Color iconColor,
    required String label,
    required String value,
  }) {
    return Row(
      children: [
        Icon(icon, color: iconColor),
        const SizedBox(width: 16),
        Expanded(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(label, style: GoogleFonts.cairo(fontSize: 12, color: AppColors.textMuted)),
              const SizedBox(height: 2),
              Text(value, style: GoogleFonts.cairo(fontSize: 16, fontWeight: FontWeight.w600)),
            ],
          ),
        ),
      ],
    );
  }

  Widget _priceCard(Quote q) {
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          children: [
            _priceRow('سعر الرحلة', '${q.ridePriceEgp.toStringAsFixed(0)} ج.م'),
            if (q.pendingFeesEgp > 0) ...[
              const SizedBox(height: 8),
              _priceRow(
                'غرامات سابقة',
                '+ ${q.pendingFeesEgp.toStringAsFixed(0)} ج.م',
                color: AppColors.warning,
              ),
            ],
            const Divider(height: 24),
            _priceRow(
              'الإجمالي',
              '${q.totalEgp.toStringAsFixed(0)} ج.م',
              bold: true,
              size: 20,
            ),
          ],
        ),
      ),
    );
  }

  Widget _priceRow(String label, String value, {Color? color, bool bold = false, double size = 15}) {
    return Row(
      mainAxisAlignment: MainAxisAlignment.spaceBetween,
      children: [
        Text(label, style: GoogleFonts.cairo(fontSize: size, color: color)),
        Text(
          value,
          style: GoogleFonts.cairo(
            fontSize: size,
            color: color ?? Colors.white,
            fontWeight: bold ? FontWeight.w900 : FontWeight.w600,
          ),
        ),
      ],
    );
  }
}
