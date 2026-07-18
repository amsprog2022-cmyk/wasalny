class RideDriver {
  final int id;
  final String name;
  final String? carModel;
  final String? carPlate;
  final String? carColor;
  final double? rating;
  final int totalTrips;

  RideDriver({
    required this.id,
    required this.name,
    this.carModel,
    this.carPlate,
    this.carColor,
    this.rating,
    this.totalTrips = 0,
  });

  factory RideDriver.fromJson(Map<String, dynamic> json) => RideDriver(
        id: json['id'] as int,
        name: json['name'] as String? ?? '',
        carModel: json['car_model'] as String?,
        carPlate: json['car_plate'] as String?,
        carColor: json['car_color'] as String?,
        rating: (json['rating'] as num?)?.toDouble(),
        totalTrips: json['total_trips'] as int? ?? 0,
      );
}

class Ride {
  final int id;
  final int customerId;
  final int? driverId;
  final int fromZoneId;
  final int toZoneId;
  final String? fromZoneAr;
  final String? toZoneAr;
  final double priceEgp;
  final double commissionEgp;
  final double noShowFeeEgp;
  final String status;
  final String source;
  final DateTime? createdAt;
  final DateTime? assignedAt;
  final DateTime? startedAt;
  final DateTime? completedAt;
  final String? cancelReason;
  final int? rating;
  final RideDriver? driver;

  Ride({
    required this.id,
    required this.customerId,
    this.driverId,
    required this.fromZoneId,
    required this.toZoneId,
    this.fromZoneAr,
    this.toZoneAr,
    required this.priceEgp,
    required this.commissionEgp,
    required this.noShowFeeEgp,
    required this.status,
    required this.source,
    this.createdAt,
    this.assignedAt,
    this.startedAt,
    this.completedAt,
    this.cancelReason,
    this.rating,
    this.driver,
  });

  factory Ride.fromJson(Map<String, dynamic> json) => Ride(
        id: json['id'] as int,
        customerId: json['customer_id'] as int,
        driverId: json['driver_id'] as int?,
        fromZoneId: json['from_zone_id'] as int,
        toZoneId: json['to_zone_id'] as int,
        fromZoneAr: json['from_zone'] as String?,
        toZoneAr: json['to_zone'] as String?,
        priceEgp: (json['price_egp'] as num).toDouble(),
        commissionEgp: (json['commission_egp'] as num?)?.toDouble() ?? 0,
        noShowFeeEgp: (json['no_show_fee_egp'] as num?)?.toDouble() ?? 0,
        status: json['status'] as String,
        source: json['source'] as String? ?? 'app',
        createdAt: _parseDate(json['created_at']),
        assignedAt: _parseDate(json['assigned_at']),
        startedAt: _parseDate(json['started_at']),
        completedAt: _parseDate(json['completed_at']),
        cancelReason: json['cancel_reason'] as String?,
        rating: json['rating'] as int?,
        driver: json['driver'] != null
            ? RideDriver.fromJson(json['driver'] as Map<String, dynamic>)
            : null,
      );

  bool get isTerminal =>
      status == 'completed' ||
      status == 'cancelled' ||
      status == 'cancelled_no_show';

  bool get isActive => !isTerminal && status != 'new';

  static DateTime? _parseDate(dynamic value) {
    if (value is String) return DateTime.tryParse(value);
    return null;
  }
}
