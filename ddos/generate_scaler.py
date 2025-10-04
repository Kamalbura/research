#!/usr/bin/env python3
"""Generate the missing scaler.pkl file required for DDoS detection system."""

import pandas as pd
from sklearn.preprocessing import StandardScaler
import joblib
from pathlib import Path

def main():
    """Generate scaler.pkl from training data."""
    
    # Check if training data exists
    train_file = Path("train_ddos_data_0.1.csv")
    if not train_file.exists():
        print(f"❌ Training data not found: {train_file}")
        print("Please ensure train_ddos_data_0.1.csv exists in the ddos/ directory")
        return 1
    
    try:
        # Load training data
        print(f"📊 Loading training data from {train_file}")
        train_df = pd.read_csv(train_file)
        
        # Check if required column exists
        if "Mavlink_Count" not in train_df.columns:
            print("❌ Column 'Mavlink_Count' not found in training data")
            print(f"Available columns: {list(train_df.columns)}")
            return 1
        
        print(f"✅ Found {len(train_df)} training samples")
        print(f"📈 Mavlink_Count range: {train_df['Mavlink_Count'].min()} - {train_df['Mavlink_Count'].max()}")
        
        # Create and fit scaler
        print("🔧 Creating StandardScaler...")
        scaler = StandardScaler()
        scaler.fit(train_df[["Mavlink_Count"]])
        
        # Save scaler
        scaler_file = Path("scaler.pkl")
        joblib.dump(scaler, scaler_file)
        
        print(f"✅ Successfully generated {scaler_file}")
        print(f"📊 Scaler parameters:")
        print(f"   - Mean: {scaler.mean_[0]:.3f}")
        print(f"   - Std:  {scaler.scale_[0]:.3f}")
        
        # Test the scaler
        print("🧪 Testing scaler...")
        test_data = [[10.0], [50.0], [100.0]]
        scaled = scaler.transform(test_data)
        print(f"   - Sample transformations: {[f'{x[0]:.3f}' for x in scaled]}")
        
        return 0
        
    except Exception as e:
        print(f"❌ Error generating scaler: {e}")
        return 1

if __name__ == "__main__":
    exit(main())