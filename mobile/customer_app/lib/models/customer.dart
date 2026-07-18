class Customer {
  final int id;
  final String waId;
  final String? name;
  final int totalTrips;
  final double totalSpentEgp;
  final double pendingFeesEgp;

  Customer({
    required this.id,
    required this.waId,
    this.name,
    this.totalTrips = 0,
    this.totalSpentEgp = 0,
    this.pendingFeesEgp = 0,
  });

  factory Customer.fromJson(Map<String, dynamic> json) => Customer(
        id: json['id'] as int,
        waId: json['wa_id'] as String,
        name: json['name'] as String?,
        totalTrips: json['total_trips'] as int? ?? 0,
        totalSpentEgp: (json['total_spent_egp'] as num?)?.toDouble() ?? 0,
        pendingFeesEgp: (json['pending_fees_egp'] as num?)?.toDouble() ?? 0,
      );
}
