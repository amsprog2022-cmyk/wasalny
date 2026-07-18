import '../models/quote.dart';
import '../models/ride.dart';
import 'api_client.dart';

class RidesService {
  final _api = ApiClient.instance;

  Future<List<dynamic>> listZones() async {
    final resp = await _api.dio.get('/api/v1/zones');
    return resp.data as List<dynamic>;
  }

  Future<Quote> quote(int fromZoneId, int toZoneId) async {
    final resp = await _api.dio.post(
      '/api/v1/rides/quote',
      data: {'from_zone_id': fromZoneId, 'to_zone_id': toZoneId},
    );
    return Quote.fromJson(resp.data as Map<String, dynamic>);
  }

  Future<Ride> createRide(int fromZoneId, int toZoneId) async {
    final resp = await _api.dio.post(
      '/api/v1/rides',
      data: {'from_zone_id': fromZoneId, 'to_zone_id': toZoneId},
    );
    return Ride.fromJson(resp.data as Map<String, dynamic>);
  }

  Future<Ride> getRide(int rideId) async {
    final resp = await _api.dio.get('/api/v1/rides/$rideId');
    return Ride.fromJson(resp.data as Map<String, dynamic>);
  }

  Future<Ride> cancelRide(int rideId, {String? reason}) async {
    final resp = await _api.dio.post(
      '/api/v1/rides/$rideId/cancel',
      data: {if (reason != null) 'reason': reason},
    );
    return Ride.fromJson(resp.data as Map<String, dynamic>);
  }

  Future<Ride> rateRide(int rideId, int stars, {String? comment}) async {
    final resp = await _api.dio.post(
      '/api/v1/rides/$rideId/rate',
      data: {'stars': stars, if (comment != null) 'comment': comment},
    );
    return Ride.fromJson(resp.data as Map<String, dynamic>);
  }

  Future<void> sos(int rideId, {String? message}) async {
    await _api.dio.post(
      '/api/v1/rides/$rideId/sos',
      data: {if (message != null) 'message': message},
    );
  }

  Future<void> fileComplaint(
    int rideId, {
    required String subject,
    String? description,
    String category = 'other',
  }) async {
    await _api.dio.post(
      '/api/v1/rides/$rideId/complaint',
      data: {
        'subject': subject,
        'category': category,
        if (description != null) 'description': description,
      },
    );
  }

  Future<List<Ride>> myRides({int limit = 20}) async {
    final resp = await _api.dio.get('/api/v1/customer/rides', queryParameters: {'limit': limit});
    return (resp.data as List)
        .map((e) => Ride.fromJson(e as Map<String, dynamic>))
        .toList();
  }
}
