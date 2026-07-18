class Zone {
  final int id;
  final String slug;
  final String nameAr;
  final String nameEn;
  final bool isActive;

  Zone({
    required this.id,
    required this.slug,
    required this.nameAr,
    required this.nameEn,
    this.isActive = true,
  });

  factory Zone.fromJson(Map<String, dynamic> json) => Zone(
        id: json['id'] as int,
        slug: json['slug'] as String,
        nameAr: json['name_ar'] as String,
        nameEn: json['name_en'] as String,
        isActive: (json['is_active'] as bool?) ?? true,
      );
}
