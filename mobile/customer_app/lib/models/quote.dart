class Quote {
  final int fromZoneId;
  final int toZoneId;
  final double ridePriceEgp;
  final double commissionEgp;
  final double pendingFeesEgp;
  final double totalEgp;
  final List<int> pendingFeeIds;

  Quote({
    required this.fromZoneId,
    required this.toZoneId,
    required this.ridePriceEgp,
    required this.commissionEgp,
    required this.pendingFeesEgp,
    required this.totalEgp,
    required this.pendingFeeIds,
  });

  factory Quote.fromJson(Map<String, dynamic> json) => Quote(
        fromZoneId: json['from_zone_id'] as int,
        toZoneId: json['to_zone_id'] as int,
        ridePriceEgp: (json['ride_price_egp'] as num).toDouble(),
        commissionEgp: (json['commission_egp'] as num).toDouble(),
        pendingFeesEgp: (json['pending_fees_egp'] as num).toDouble(),
        totalEgp: (json['total_egp'] as num).toDouble(),
        pendingFeeIds:
            (json['pending_fee_ids'] as List?)?.cast<int>() ?? const [],
      );
}
