// Change the baseUrl here to point to a different backend (staging vs prod).
// Passed at build time via --dart-define=BASE_URL=https://your-url.up.railway.app
class AppConfig {
  static const baseUrl = String.fromEnvironment(
    'BASE_URL',
    defaultValue: 'https://web-production-c44b3.up.railway.app',
  );

  static const socketUrl = String.fromEnvironment(
    'SOCKET_URL',
    defaultValue: 'https://web-production-c44b3.up.railway.app',
  );

  // Local dev: run `flutter run --dart-define=BASE_URL=http://10.0.2.2:5000`
  // on Android emulator, or `http://localhost:5000` on iOS simulator.
}
