import 'package:flutter/material.dart';
import 'package:google_fonts/google_fonts.dart';

import '../config/theme.dart';

class WassalnyLogo extends StatelessWidget {
  final double size;
  final bool showTagline;
  const WassalnyLogo({super.key, this.size = 80, this.showTagline = false});

  @override
  Widget build(BuildContext context) {
    return Column(
      mainAxisSize: MainAxisSize.min,
      children: [
        Text('🚗', style: TextStyle(fontSize: size)),
        const SizedBox(height: 8),
        Text(
          'وصلني بنها',
          style: GoogleFonts.cairo(
            fontSize: size * 0.36,
            fontWeight: FontWeight.w900,
            color: AppColors.primary,
          ),
        ),
        if (showTagline) ...[
          const SizedBox(height: 4),
          Text(
            'أقرب كابتن هيكلمك',
            style: GoogleFonts.cairo(
              fontSize: 14,
              color: AppColors.textMuted,
            ),
          ),
        ],
      ],
    );
  }
}
