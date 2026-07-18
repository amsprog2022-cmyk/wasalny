import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:shared_preferences/shared_preferences.dart';

import '../services/socket_service.dart';
import '../state/auth_provider.dart';
import '../widgets/wassalny_logo.dart';
import 'home_screen.dart';
import 'login_screen.dart';
import 'onboarding_screen.dart';

class SplashScreen extends ConsumerStatefulWidget {
  const SplashScreen({super.key});

  @override
  ConsumerState<SplashScreen> createState() => _SplashScreenState();
}

class _SplashScreenState extends ConsumerState<SplashScreen> {
  @override
  void initState() {
    super.initState();
    _boot();
  }

  Future<void> _boot() async {
    await Future.wait([
      ref.read(authProvider.notifier).bootstrap(),
      Future.delayed(const Duration(milliseconds: 900)),
    ]);
    if (!mounted) return;

    final prefs = await SharedPreferences.getInstance();
    final seenOnboarding = prefs.getBool('seen_onboarding') ?? false;

    final auth = ref.read(authProvider);
    if (auth.isLoggedIn) {
      await CustomerSocket.instance.connect();
      _push(const HomeScreen());
    } else if (!seenOnboarding) {
      _push(const OnboardingScreen());
    } else {
      _push(const LoginScreen());
    }
  }

  void _push(Widget screen) {
    Navigator.of(context).pushReplacement(
      MaterialPageRoute(builder: (_) => screen),
    );
  }

  @override
  Widget build(BuildContext context) {
    return const Scaffold(
      body: Center(child: WassalnyLogo(size: 100, showTagline: true)),
    );
  }
}
