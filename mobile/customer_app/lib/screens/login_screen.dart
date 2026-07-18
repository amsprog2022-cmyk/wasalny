import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:google_fonts/google_fonts.dart';

import '../config/theme.dart';
import '../services/socket_service.dart';
import '../state/auth_provider.dart';
import '../widgets/primary_button.dart';
import '../widgets/wassalny_logo.dart';
import 'home_screen.dart';

class LoginScreen extends ConsumerStatefulWidget {
  const LoginScreen({super.key});

  @override
  ConsumerState<LoginScreen> createState() => _LoginScreenState();
}

class _LoginScreenState extends ConsumerState<LoginScreen> {
  final _phoneCtrl = TextEditingController();
  final _nameCtrl = TextEditingController();
  bool _submitting = false;

  String _normalize(String raw) {
    raw = raw.replaceAll(' ', '').replaceAll('-', '');
    if (raw.startsWith('+')) raw = raw.substring(1);
    if (raw.startsWith('00')) raw = raw.substring(2);
    if (raw.startsWith('0')) return '20${raw.substring(1)}';
    if (raw.startsWith('20')) return raw;
    return raw;
  }

  Future<void> _submit() async {
    final phone = _phoneCtrl.text.trim();
    final name = _nameCtrl.text.trim();
    if (phone.length < 8) {
      _snack('اكتب رقم موبايل صحيح');
      return;
    }
    setState(() => _submitting = true);
    final ok = await ref
        .read(authProvider.notifier)
        .login(_normalize(phone), name: name.isEmpty ? null : name);
    setState(() => _submitting = false);
    if (!mounted) return;
    if (ok) {
      await CustomerSocket.instance.connect();
      if (!mounted) return;
      Navigator.of(context).pushReplacement(
        MaterialPageRoute(builder: (_) => const HomeScreen()),
      );
    } else {
      _snack('حصل خطأ. جرب تاني.');
    }
  }

  void _snack(String msg) {
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(content: Text(msg, style: GoogleFonts.cairo())),
    );
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: SafeArea(
        child: SingleChildScrollView(
          padding: const EdgeInsets.symmetric(horizontal: 24, vertical: 48),
          child: Column(
            children: [
              const WassalnyLogo(size: 80, showTagline: true),
              const SizedBox(height: 48),
              Text(
                'يلا نبدأ',
                style: GoogleFonts.cairo(
                  fontSize: 22,
                  fontWeight: FontWeight.w700,
                ),
              ),
              const SizedBox(height: 8),
              Text(
                'اكتب اسمك ورقم موبايلك عشان نبدأ',
                style: GoogleFonts.cairo(color: AppColors.textMuted),
              ),
              const SizedBox(height: 32),
              TextField(
                controller: _nameCtrl,
                decoration: const InputDecoration(
                  labelText: 'الاسم',
                  hintText: 'مثال: أحمد فخري',
                ),
                textInputAction: TextInputAction.next,
              ),
              const SizedBox(height: 16),
              TextField(
                controller: _phoneCtrl,
                keyboardType: TextInputType.phone,
                textDirection: TextDirection.ltr,
                inputFormatters: [
                  FilteringTextInputFormatter.digitsOnly,
                  LengthLimitingTextInputFormatter(15),
                ],
                decoration: const InputDecoration(
                  labelText: 'رقم الموبايل',
                  hintText: '01029188887',
                ),
              ),
              const SizedBox(height: 32),
              SizedBox(
                width: double.infinity,
                child: PrimaryButton(
                  label: 'دخول',
                  loading: _submitting,
                  onPressed: _submit,
                ),
              ),
              const SizedBox(height: 12),
              Text(
                'بضغطك على دخول، أنت توافق على شروط الخدمة وسياسة الخصوصية.',
                textAlign: TextAlign.center,
                style: GoogleFonts.cairo(
                  fontSize: 12,
                  color: AppColors.textMuted,
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
