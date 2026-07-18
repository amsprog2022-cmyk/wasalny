import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../models/customer.dart';
import '../services/api_client.dart';
import '../services/auth_service.dart';

final authServiceProvider = Provider<AuthService>((ref) => AuthService());

/// nullable Customer — null means logged out.
class AuthState {
  final Customer? customer;
  final bool isLoading;
  final String? error;

  const AuthState({this.customer, this.isLoading = false, this.error});

  bool get isLoggedIn => customer != null;

  AuthState copyWith({Customer? customer, bool? isLoading, String? error, bool clearCustomer = false}) {
    return AuthState(
      customer: clearCustomer ? null : (customer ?? this.customer),
      isLoading: isLoading ?? this.isLoading,
      error: error,
    );
  }
}

class AuthNotifier extends StateNotifier<AuthState> {
  AuthNotifier(this._service) : super(const AuthState());

  final AuthService _service;

  Future<void> bootstrap() async {
    state = state.copyWith(isLoading: true);
    final me = await _service.tryFetchMe();
    state = AuthState(customer: me, isLoading: false);
  }

  Future<bool> login(String waId, {String? name}) async {
    state = state.copyWith(isLoading: true);
    try {
      final result = await _service.loginByPhone(waId, name: name);
      state = AuthState(customer: result.customer);
      return true;
    } catch (e) {
      state = state.copyWith(isLoading: false, error: e.toString());
      return false;
    }
  }

  Future<void> updateName(String name) async {
    final updated = await _service.updateName(name);
    state = state.copyWith(customer: updated);
  }

  Future<void> logout() async {
    await ApiClient.instance.clearToken();
    state = const AuthState();
  }
}

final authProvider = StateNotifierProvider<AuthNotifier, AuthState>(
  (ref) => AuthNotifier(ref.read(authServiceProvider)),
);
