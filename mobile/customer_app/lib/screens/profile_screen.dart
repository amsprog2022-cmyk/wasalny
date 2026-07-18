import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:google_fonts/google_fonts.dart';

import '../config/theme.dart';
import '../services/socket_service.dart';
import '../state/auth_provider.dart';
import 'login_screen.dart';

class ProfileScreen extends ConsumerStatefulWidget {
  const ProfileScreen({super.key});

  @override
  ConsumerState<ProfileScreen> createState() => _ProfileScreenState();
}

class _ProfileScreenState extends ConsumerState<ProfileScreen> {
  Future<void> _editName() async {
    final ctrl = TextEditingController(text: ref.read(authProvider).customer?.name ?? '');
    final result = await showDialog<String>(
      context: context,
      builder: (_) => AlertDialog(
        backgroundColor: AppColors.surface,
        title: Text('غيّر اسمك', style: GoogleFonts.cairo()),
        content: TextField(controller: ctrl, autofocus: true),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context),
            child: Text('إلغاء', style: GoogleFonts.cairo(color: AppColors.textMuted)),
          ),
          TextButton(
            onPressed: () => Navigator.pop(context, ctrl.text.trim()),
            child: Text('احفظ', style: GoogleFonts.cairo(color: AppColors.primary)),
          ),
        ],
      ),
    );
    if (result != null && result.isNotEmpty) {
      await ref.read(authProvider.notifier).updateName(result);
    }
  }

  Future<void> _logout() async {
    await ref.read(authProvider.notifier).logout();
    CustomerSocket.instance.disconnect();
    if (!mounted) return;
    Navigator.of(context).pushAndRemoveUntil(
      MaterialPageRoute(builder: (_) => const LoginScreen()),
      (_) => false,
    );
  }

  @override
  Widget build(BuildContext context) {
    final c = ref.watch(authProvider).customer;
    return Scaffold(
      appBar: AppBar(title: const Text('حسابي')),
      body: SafeArea(
        child: ListView(
          padding: const EdgeInsets.all(20),
          children: [
            Card(
              child: Padding(
                padding: const EdgeInsets.all(20),
                child: Column(
                  children: [
                    CircleAvatar(
                      radius: 36,
                      backgroundColor: AppColors.surfaceRaised,
                      child: Text(
                        c?.name?.characters.firstOrNull ?? '؟',
                        style: GoogleFonts.cairo(
                          fontSize: 32,
                          fontWeight: FontWeight.w900,
                          color: AppColors.primary,
                        ),
                      ),
                    ),
                    const SizedBox(height: 12),
                    Text(
                      c?.name ?? '—',
                      style: GoogleFonts.cairo(fontSize: 20, fontWeight: FontWeight.w900),
                    ),
                    Text(
                      c?.waId ?? '—',
                      style: GoogleFonts.cairo(color: AppColors.textMuted),
                    ),
                    const SizedBox(height: 8),
                    TextButton(
                      onPressed: _editName,
                      child: Text('غيّر اسمك', style: GoogleFonts.cairo(color: AppColors.primary)),
                    ),
                  ],
                ),
              ),
            ),
            const SizedBox(height: 16),
            Row(
              children: [
                Expanded(child: _statCard(label: 'رحلات', value: '${c?.totalTrips ?? 0}')),
                const SizedBox(width: 12),
                Expanded(
                  child: _statCard(
                    label: 'صرفت',
                    value: '${(c?.totalSpentEgp ?? 0).toStringAsFixed(0)} ج.م',
                  ),
                ),
              ],
            ),
            if ((c?.pendingFeesEgp ?? 0) > 0) ...[
              const SizedBox(height: 12),
              Card(
                color: AppColors.warning.withValues(alpha: 0.15),
                child: Padding(
                  padding: const EdgeInsets.all(16),
                  child: Row(
                    children: [
                      const Icon(Icons.warning_amber_rounded, color: AppColors.warning),
                      const SizedBox(width: 12),
                      Expanded(
                        child: Text(
                          'عليك ${c!.pendingFeesEgp.toStringAsFixed(0)} ج.م غرامة هتتحسب في الرحلة اللي جاية',
                          style: GoogleFonts.cairo(color: AppColors.warning),
                        ),
                      ),
                    ],
                  ),
                ),
              ),
            ],
            const SizedBox(height: 24),
            Card(
              child: Column(
                children: [
                  ListTile(
                    leading: const Icon(Icons.logout, color: AppColors.statusCancelled),
                    title: Text(
                      'تسجيل خروج',
                      style: GoogleFonts.cairo(color: AppColors.statusCancelled),
                    ),
                    onTap: _logout,
                  ),
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }

  Widget _statCard({required String label, required String value}) {
    return Card(
      child: Padding(
        padding: const EdgeInsets.symmetric(vertical: 20),
        child: Column(
          children: [
            Text(value, style: GoogleFonts.cairo(fontSize: 24, fontWeight: FontWeight.w900)),
            const SizedBox(height: 4),
            Text(label, style: GoogleFonts.cairo(color: AppColors.textMuted, fontSize: 12)),
          ],
        ),
      ),
    );
  }
}
