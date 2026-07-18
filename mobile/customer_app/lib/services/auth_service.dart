import 'package:dio/dio.dart';

import '../models/customer.dart';
import 'api_client.dart';

class AuthResult {
  final String accessToken;
  final Customer customer;
  AuthResult(this.accessToken, this.customer);
}

class AuthService {
  final _api = ApiClient.instance;

  Future<AuthResult> loginByPhone(String waId, {String? name}) async {
    final resp = await _api.dio.post(
      '/api/v1/customer/login',
      data: {'wa_id': waId, if (name != null) 'name': name},
    );
    final data = resp.data as Map<String, dynamic>;
    final token = data['access_token'] as String;
    await _api.setToken(token);
    return AuthResult(token, Customer.fromJson(data['customer'] as Map<String, dynamic>));
  }

  Future<Customer?> tryFetchMe() async {
    final token = await _api.getToken();
    if (token == null || token.isEmpty) return null;
    try {
      final resp = await _api.dio.get('/api/v1/customer/me');
      return Customer.fromJson(resp.data as Map<String, dynamic>);
    } on DioException {
      return null;
    }
  }

  Future<Customer> updateName(String name) async {
    final resp = await _api.dio.patch(
      '/api/v1/customer/me',
      data: {'name': name},
    );
    return Customer.fromJson(resp.data as Map<String, dynamic>);
  }

  Future<void> logout() => _api.clearToken();
}
